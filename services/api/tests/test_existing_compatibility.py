import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from detector import AegisDetector
from policy import RemediationPolicy
from workflow import StepFunctionsWorkflowManager
from remediation import RemediationEngine


def test_detector_static_and_isolation_forest():
    """13. Existing detector functionality verification."""
    detector = AegisDetector(min_samples=10, max_history=60, contamination=0.05, random_state=42)

    # Static threshold
    res = detector.check_metric("cpu", 96.0)
    assert res["anomaly"] is True
    assert res["severity"] == "CRITICAL"
    assert res["detection_method"] == "STATIC_THRESHOLD"

    # Isolation Forest anomaly
    detector.reset_history()
    detector.add_history("cpu", [2.0, 2.2, 2.1, 2.4, 2.3, 2.0, 2.2, 2.5, 2.1, 2.3, 2.2, 2.4])
    ml_res = detector.check_metric("cpu", 60.0)
    assert ml_res["anomaly"] is True
    assert ml_res["detection_method"] == "ISOLATION_FOREST"


def test_policy_guardrails_all_cases():
    """14. Existing policy safety gates (cooldown, max desired, critical-only)."""
    policy = RemediationPolicy(max_desired_count=3, cooldown_seconds=60, critical_only=True)

    # Non-critical blocked
    inc_high = {"id": "INC-1", "severity": "HIGH"}
    dec1 = policy.evaluate(inc_high, "SCALE_OUT", current_desired_count=1)
    assert dec1["allowed"] is False
    assert "CRITICAL required" in dec1["reason"]

    # Critical allowed
    inc_crit = {"id": "INC-2", "severity": "CRITICAL"}
    dec2 = policy.evaluate(inc_crit, "SCALE_OUT", current_desired_count=1)
    assert dec2["allowed"] is True

    # Max count reached
    dec3 = policy.evaluate(inc_crit, "SCALE_OUT", current_desired_count=3)
    assert dec3["allowed"] is False
    assert "Maximum desired task count reached" in dec3["reason"]

    # Cooldown active
    policy.record_action()
    dec4 = policy.evaluate(inc_crit, "SCALE_OUT", current_desired_count=1)
    assert dec4["allowed"] is False
    assert "Cooldown active" in dec4["reason"]


def test_step_functions_workflow_manager():
    """15. Existing Step Functions workflow execution and polling logic."""
    mgr = StepFunctionsWorkflowManager()
    mgr.sfn = MagicMock()
    mgr._state_machine_arn = "arn:aws:states:ap-south-1:123456789012:stateMachine:aegis-recovery-workflow"

    mgr.sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:ap-south-1:123456789012:execution:aegis-recovery-workflow:test-exec",
        "startDate": MagicMock(isoformat=lambda: "2026-09-19T00:00:00Z")
    }

    exec_info = mgr.start_recovery({"id": "INC-01", "severity": "CRITICAL"}, "SCALE_OUT", 2)
    assert "execution_arn" in exec_info

    mgr.sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "output": '{"workflow_status": "SUCCESS", "recovery_verified": true}'
    }

    result = mgr.wait_for_completion(exec_info["execution_arn"], timeout_seconds=5)
    assert result["workflow_status"] == "SUCCESS"
    assert result["recovery_verified"] is True


def test_remediation_engine_scale_out_and_restart():
    """16. Existing remediation engine commands."""
    policy = RemediationPolicy(cooldown_seconds=0)
    engine = RemediationEngine(policy=policy)
    engine.ecs = MagicMock()
    engine.ecs.describe_services.return_value = {
        "services": [{"desiredCount": 1, "runningCount": 1}]
    }

    inc = {"id": "INC-REM-01", "severity": "CRITICAL"}
    # Execute=False blocked by safety flag
    res_dry = engine.scale_out(inc, execute=False)
    assert res_dry["action"] == "SCALE_OUT"
    assert res_dry["status"] == "BLOCKED_BY_SAFETY_POLICY"

    # Execute=True executed successfully
    res_exec = engine.scale_out(inc, execute=True)
    assert res_exec["action"] == "SCALE_OUT"
    assert res_exec["target_desired_count"] == 2
    assert res_exec["status"] == "EXECUTED"
    assert res_exec["policy"]["allowed"] is True
