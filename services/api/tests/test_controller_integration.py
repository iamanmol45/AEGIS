import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from controller import AegisController
from policy import RemediationPolicy
from correlation import IncidentCorrelationEngine
from incident import IncidentManager
from incident_store import IncidentStore


def test_controller_correlated_multi_signal_flow():
    """Verify that multiple simultaneous anomalies (e.g. CPU + Memory) flow into one correlated incident."""
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)
    mock_workflow = MagicMock()
    mock_workflow.start_recovery.return_value = {
        "execution_arn": "arn:aws:states:ap-south-1:123456789012:execution:aegis-recovery-workflow:test-exec"
    }
    mock_workflow.wait_for_completion.return_value = {
        "workflow_status": "SUCCESS",
        "recovery_verified": True
    }

    store = IncidentStore(table_name="TestTable")
    incident_manager = IncidentManager(store=store)
    correlation_engine = IncidentCorrelationEngine(window_seconds=60)

    controller = AegisController(
        policy=policy,
        workflow=mock_workflow,
        correlation_engine=correlation_engine,
        incident_manager=incident_manager,
    )

    controller.remediation.ecs = MagicMock()
    controller.remediation.ecs.describe_services.return_value = {
        "services": [{"desiredCount": 1, "runningCount": 1, "pendingCount": 0}]
    }

    def mock_get_latest_metric(*args, **kwargs):
        m = kwargs.get("metric_name") or (args[0] if args else "")
        if m == "CPUUtilization":
            return 96.0
        elif m == "MemoryUtilization":
            return 92.0
        return 10.0

    controller.detector.get_latest_metric = MagicMock(side_effect=mock_get_latest_metric)

    # Run controller cycle
    result = controller.run()

    assert result["anomaly"] is True
    assert result["correlated"] is True
    assert result["incident"].severity == "CRITICAL"
    assert result["incident"].signal_count == 2
    assert result["policy"]["allowed"] is True
    mock_workflow.start_recovery.assert_called_once()
    assert result["workflow"]["workflow_status"] == "SUCCESS"


def test_controller_correlated_blocked_by_policy():
    """Verify that if policy disallows (e.g. max desired reached), correlated incident is blocked safely."""
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)
    mock_workflow = MagicMock()

    correlation_engine = IncidentCorrelationEngine(window_seconds=60)
    controller = AegisController(
        policy=policy,
        workflow=mock_workflow,
        correlation_engine=correlation_engine,
    )

    controller.remediation.ecs = MagicMock()
    controller.remediation.ecs.describe_services.return_value = {
        "services": [{"desiredCount": 4, "runningCount": 4, "pendingCount": 0}]
    }

    def mock_get_latest_metric(*args, **kwargs):
        m = kwargs.get("metric_name") or (args[0] if args else "")
        if m == "CPUUtilization":
            return 96.0
        elif m == "MemoryUtilization":
            return 92.0
        return 10.0

    controller.detector.get_latest_metric = MagicMock(side_effect=mock_get_latest_metric)

    result = controller.run()

    assert result["anomaly"] is True
    assert result["correlated"] is True
    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["policy"]["allowed"] is False
    assert "Maximum desired task count reached" in result["policy"]["reason"]
    mock_workflow.start_recovery.assert_not_called()
