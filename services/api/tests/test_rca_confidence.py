import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rca import RCAEngine
from policy import RemediationPolicy
from incident_store import IncidentStore


def _empty_store():
    """IncidentStore against a nonexistent table -- get_incidents() falls
    back to the empty in-memory mirror, so RCAEngine sees no precedent."""
    return IncidentStore(table_name="NonexistentTestTable")


def test_known_single_signal_metric_has_moderate_confidence():
    rca = RCAEngine(store=_empty_store())
    result = rca.analyze({"id": "INC-1", "metric": "cpu", "value": 95, "threshold": 80, "severity": "HIGH"})
    assert result["root_cause"]
    assert 0.6 <= result["confidence"] < 0.9


def test_correlated_multi_signal_boosts_confidence_over_single_signal():
    rca = RCAEngine(store=_empty_store())
    single = rca.analyze({"id": "INC-2", "metric": "cpu", "value": 95, "threshold": 80, "signal_count": 1})
    correlated = rca.analyze({"id": "INC-3", "metric": "correlated_cpu_memory", "value": 95, "threshold": 0, "signal_count": 2})
    assert correlated["confidence"] > single["confidence"]


def test_novel_unrecognized_metric_gets_low_confidence():
    rca = RCAEngine(store=_empty_store())
    result = rca.analyze({"id": "INC-4", "metric": "disk_io_saturation", "value": 999, "threshold": 100})
    assert result["confidence"] < 0.6


def test_policy_blocks_low_confidence_even_when_otherwise_eligible():
    """T09: a CRITICAL, whitelisted, in-budget action must still be
    escalated (blocked) if the RCA confidence behind it is too low."""
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)

    high_confidence = policy.evaluate(
        incident={"id": "INC-5", "severity": "CRITICAL"},
        action="SCALE_OUT",
        current_desired_count=1,
        confidence=0.9,
    )
    assert high_confidence["allowed"] is True

    low_confidence = policy.evaluate(
        incident={"id": "INC-6", "severity": "CRITICAL"},
        action="SCALE_OUT",
        current_desired_count=1,
        confidence=0.35,
    )
    assert low_confidence["allowed"] is False
    assert "confidence" in low_confidence["reason"].lower()


def test_policy_default_confidence_preserves_prior_behavior():
    """Callers that don't pass confidence (existing code paths) must not
    be newly blocked -- default confidence=1.0 always clears the gate."""
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)
    decision = policy.evaluate(
        incident={"id": "INC-7", "severity": "CRITICAL"},
        action="SCALE_OUT",
        current_desired_count=1,
    )
    assert decision["allowed"] is True
