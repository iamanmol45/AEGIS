import pytest
from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from correlation import IncidentCorrelationEngine


@pytest.fixture
def engine():
    return IncidentCorrelationEngine(window_seconds=60)


def test_single_signal_does_not_create_correlation(engine):
    """1. Single signal does not create correlation."""
    now = datetime.utcnow()
    engine.add_signal({
        "metric": "cpu",
        "value": 92.0,
        "threshold": 80.0,
        "severity": "HIGH",
        "timestamp": now.isoformat(),
        "detection_method": "STATIC_THRESHOLD",
    })

    result = engine.correlate(now=now)
    assert result is None
    assert engine.get_pending_signals_count(now=now) == 1


def test_cpu_and_memory_within_window_create_correlated_incident(engine):
    """2. CPU + memory within 60 seconds create one correlated incident."""
    now = datetime.utcnow()
    engine.add_signal({
        "metric": "cpu",
        "value": 95.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": now.isoformat(),
        "detection_method": "STATIC_THRESHOLD",
    })
    engine.add_signal({
        "metric": "memory",
        "value": 88.0,
        "threshold": 80.0,
        "severity": "HIGH",
        "timestamp": (now + timedelta(seconds=15)).isoformat(),
        "detection_method": "STATIC_THRESHOLD",
    })

    result = engine.correlate(now=now + timedelta(seconds=20))
    assert result is not None
    assert result["correlated"] is True
    assert result["signal_count"] == 2
    assert result["severity"] == "CRITICAL"
    assert result["incident_id"].startswith("INC-CORR-")
    metrics = [s["metric"] for s in result["signals"]]
    assert "cpu" in metrics
    assert "memory" in metrics


def test_cpu_and_task_failure_create_correlated_incident(engine):
    """3. CPU + task_failure within 60 seconds create one correlated incident."""
    now = datetime.utcnow()
    engine.add_signal({
        "metric": "cpu",
        "value": 96.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": now.isoformat(),
    })
    engine.add_signal({
        "metric": "task_failure",
        "value": 0,
        "threshold": 1,
        "severity": "CRITICAL",
        "timestamp": (now + timedelta(seconds=10)).isoformat(),
    })

    result = engine.correlate(now=now + timedelta(seconds=15))
    assert result is not None
    assert result["correlated"] is True
    assert result["signal_count"] == 2
    assert result["severity"] == "CRITICAL"
    metrics = [s["metric"] for s in result["signals"]]
    assert "cpu" in metrics
    assert "task_failure" in metrics


def test_signals_outside_window_do_not_correlate(engine):
    """4. Signals outside the correlation window do not correlate."""
    t0 = datetime(2026, 9, 19, 10, 0, 0)
    # Signal 1 at t0
    engine.add_signal({
        "metric": "cpu",
        "value": 95.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": t0.isoformat(),
    })

    # Signal 2 arrives 75 seconds later (window is 60s)
    t1 = t0 + timedelta(seconds=75)
    engine.add_signal({
        "metric": "memory",
        "value": 90.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": t1.isoformat(),
    })

    # At t1, Signal 1 has expired (age 75s > 60s window)
    result = engine.correlate(now=t1)
    assert result is None
    # Only the recent signal remains active
    assert engine.get_pending_signals_count(now=t1) == 1


def test_critical_signal_causes_correlated_severity_critical(engine):
    """5. CRITICAL signal causes correlated severity to be CRITICAL."""
    now = datetime.utcnow()
    engine.add_signal({
        "metric": "cpu",
        "value": 82.0,
        "threshold": 80.0,
        "severity": "HIGH",
        "timestamp": now.isoformat(),
    })
    engine.add_signal({
        "metric": "memory",
        "value": 98.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": (now + timedelta(seconds=5)).isoformat(),
    })

    result = engine.correlate(now=now + timedelta(seconds=10))
    assert result is not None
    assert result["severity"] == "CRITICAL"


def test_duplicate_signals_do_not_repeatedly_create_correlated_incident(engine):
    """6. Duplicate signals do not repeatedly create the same correlated incident."""
    now = datetime.utcnow()
    engine.add_signal({
        "metric": "cpu",
        "value": 95.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": now.isoformat(),
    })
    engine.add_signal({
        "metric": "memory",
        "value": 90.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "timestamp": (now + timedelta(seconds=5)).isoformat(),
    })

    # First correlation consumes both signals
    result1 = engine.correlate(now=now + timedelta(seconds=10))
    assert result1 is not None

    # Immediate second correlation should return None because signals were consumed
    result2 = engine.correlate(now=now + timedelta(seconds=12))
    assert result2 is None


def test_expired_signals_are_removed(engine):
    """7. Expired signals are removed."""
    t0 = datetime(2026, 9, 19, 10, 0, 0)
    engine.add_signal({
        "metric": "cpu",
        "value": 85.0,
        "threshold": 80.0,
        "severity": "HIGH",
        "timestamp": t0.isoformat(),
    })

    assert len(engine.signals) == 1
    # Check after 90 seconds
    removed = engine.clear_expired(now=t0 + timedelta(seconds=90))
    assert removed == 1
    assert len(engine.signals) == 0
