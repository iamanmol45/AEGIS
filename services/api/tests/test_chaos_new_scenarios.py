import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from chaos import ChaosTestManager
from policy import RemediationPolicy
from incident import IncidentManager
from incident_store import IncidentStore
from correlation import IncidentCorrelationEngine
from audit import AuditLogger


@pytest.fixture
def chaos_manager():
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)
    store = IncidentStore(table_name="AegisIncidentsTestScenarios")
    incident_manager = IncidentManager(store=store)
    correlation_engine = IncidentCorrelationEngine(window_seconds=60)
    audit = AuditLogger()

    mock_workflow = MagicMock()
    mock_workflow.start_recovery.return_value = {
        "execution_arn": "arn:aws:states:ap-south-1:123456789012:execution:aegis-recovery-workflow:chaos-exec"
    }
    mock_workflow.wait_for_completion.return_value = {
        "workflow_status": "SUCCESS",
        "recovery_verified": True,
    }

    mock_remediation = MagicMock()
    mock_remediation.ecs.describe_services.return_value = {
        "services": [{"desiredCount": 1, "runningCount": 1, "pendingCount": 0}]
    }

    mock_recovery = MagicMock()
    mock_recovery.verify.return_value = {
        "desired_count": 2,
        "running_count": 2,
        "pending_count": 0,
        "recovered": True,
    }

    return ChaosTestManager(
        policy=policy,
        workflow=mock_workflow,
        correlation_engine=correlation_engine,
        incident_manager=incident_manager,
        remediation=mock_remediation,
        recovery=mock_recovery,
        audit=audit,
    )


def test_latency_spike_recovers(chaos_manager):
    result = chaos_manager.run_latency_spike()
    assert result["incident"]["metric"] == "latency_ms"
    assert result["policy"]["allowed"] is True
    assert result["test"]["status"] == "COMPLETED"


def test_error_rate_spike_recovers(chaos_manager):
    result = chaos_manager.run_error_rate_spike()
    assert result["incident"]["metric"] == "http_5xx_rate"
    assert result["policy"]["allowed"] is True


def test_db_connectivity_failure_escalates_not_auto_remediates(chaos_manager):
    """T06: no ECS action fixes a DB-layer fault -- must block, never execute."""
    result = chaos_manager.run_db_connectivity_failure()
    assert result["incident"]["metric"] == "db_connection_errors"
    assert result["policy"]["allowed"] is False
    assert result["test"]["status"] == "BLOCKED_BY_POLICY"
    assert "not in allowed actions" in result["policy"]["reason"]


def test_bad_deployment_tags_recent_change_context(chaos_manager):
    result = chaos_manager.run_bad_deployment()
    assert result["context"]["recent_change"] == "deployment"
    assert result["incident"]["metric"] == "http_5xx_rate"


def test_low_confidence_incident_escalates(chaos_manager):
    """T09: an unrecognized metric pattern must escalate even though the
    action (SCALE_OUT) and severity are otherwise eligible."""
    result = chaos_manager.run_low_confidence_incident()
    assert result["rca"]["confidence"] < chaos_manager.policy.min_confidence
    assert result["policy"]["allowed"] is False
    assert "confidence" in result["policy"]["reason"].lower()


def test_normal_workload_raises_no_incident(chaos_manager):
    """T11: healthy metrics must not create an incident (no false positive)."""
    result = chaos_manager.run_normal_workload()
    assert result["test"]["status"] == "NO_ANOMALY_DETECTED"
    assert "incident" not in result


def test_all_ten_scenarios_are_listed(chaos_manager):
    tests = chaos_manager.get_available_tests()
    assert len(tests) == 10
    assert "DB_CONNECTIVITY_FAILURE" in tests
    assert "LOW_CONFIDENCE_INCIDENT" in tests
    assert "NORMAL_WORKLOAD" in tests
