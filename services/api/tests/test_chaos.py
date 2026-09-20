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
def mock_chaos_setup():
    policy = RemediationPolicy(max_desired_count=4, cooldown_seconds=0, critical_only=True)
    store = IncidentStore(table_name="AegisIncidentsTest")
    incident_manager = IncidentManager(store=store)
    correlation_engine = IncidentCorrelationEngine(window_seconds=60)
    audit = AuditLogger()

    mock_workflow = MagicMock()
    mock_workflow.start_recovery.return_value = {
        "execution_arn": "arn:aws:states:ap-south-1:123456789012:execution:aegis-recovery-workflow:chaos-exec"
    }
    mock_workflow.wait_for_completion.return_value = {
        "workflow_status": "SUCCESS",
        "recovery_verified": True
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
        "recovered": True
    }

    manager = ChaosTestManager(
        policy=policy,
        workflow=mock_workflow,
        correlation_engine=correlation_engine,
        incident_manager=incident_manager,
        remediation=mock_remediation,
        recovery=mock_recovery,
        audit=audit,
    )

    return {
        "manager": manager,
        "policy": policy,
        "workflow": mock_workflow,
        "recovery": mock_recovery,
        "remediation": mock_remediation,
    }


def test_cpu_spike_generates_anomaly_and_full_chain(mock_chaos_setup):
    """1. CPU spike generates anomaly, incident, policy approval, workflow execution, recovery verification."""
    manager = mock_chaos_setup["manager"]
    res = manager.run_cpu_spike(dry_run=False)

    assert res["test"]["scenario"] == "CPU_SPIKE"
    assert res["test"]["status"] == "COMPLETED"
    assert res["incident"]["metric"] == "cpu"
    assert res["incident"]["value"] == 95.0
    assert res["incident"]["severity"] == "CRITICAL"
    assert res["policy"]["allowed"] is True
    assert res["self_healing"]["healed"] is True
    assert res["self_healing"]["attempts"][0]["action"] == "SCALE_OUT"
    assert res["self_healing"]["attempts"][0]["recovery"]["desired_count"] == 2
    mock_chaos_setup["workflow"].start_recovery.assert_called_once()
    assert res["recovery"]["recovered"] is True


def test_task_failure_generates_critical_incident(mock_chaos_setup):
    """2. Task failure generates CRITICAL incident and triggers RESTART_TASKS."""
    manager = mock_chaos_setup["manager"]
    res = manager.run_task_failure(dry_run=False)

    assert res["test"]["scenario"] == "TASK_FAILURE"
    assert res["incident"]["metric"] == "task_failure"
    assert res["incident"]["severity"] == "CRITICAL"
    assert res["policy"]["allowed"] is True
    assert res["remediation"]["action"] == "RESTART_TASKS"


def test_memory_pressure_generates_anomaly(mock_chaos_setup):
    """3. Memory pressure generates anomaly and triggers SCALE_OUT."""
    manager = mock_chaos_setup["manager"]
    res = manager.run_memory_pressure(dry_run=False)

    assert res["test"]["scenario"] == "MEMORY_PRESSURE"
    assert res["incident"]["metric"] == "memory"
    assert res["incident"]["severity"] == "CRITICAL"
    assert res["self_healing"]["healed"] is True
    assert res["self_healing"]["attempts"][0]["action"] == "SCALE_OUT"


def test_multi_signal_scenario_contains_all_expected_signals(mock_chaos_setup):
    """4. Multi-signal scenario correlates CPU, memory, and task_failure signals into one unified incident."""
    manager = mock_chaos_setup["manager"]
    res = manager.run_multi_signal(dry_run=False)

    assert res["test"]["scenario"] == "MULTI_SIGNAL_INCIDENT"
    assert res["incident"]["incident_type"] == "CORRELATED"
    metrics = [s["metric"] for s in res["incident"]["signals"]]
    assert "cpu" in metrics
    assert "memory" in metrics
    assert "task_failure" in metrics
    assert res["incident"]["severity"] == "CRITICAL"
    # Task failure present in signals -> prioritized action is RESTART_TASKS
    assert res["remediation"]["action"] == "RESTART_TASKS"


def test_policy_gate_evaluated_and_blocks_when_disallowed(mock_chaos_setup):
    """5 & 6. Policy gate is evaluated and blocks remediation when guardrails (e.g. max count) are violated."""
    manager = mock_chaos_setup["manager"]
    policy = mock_chaos_setup["policy"]
    workflow = mock_chaos_setup["workflow"]

    # Set desired count to maximum (4)
    mock_chaos_setup["remediation"].ecs.describe_services.return_value = {
        "services": [{"desiredCount": 4, "runningCount": 4, "pendingCount": 0}]
    }

    res = manager.run_cpu_spike(dry_run=False)

    assert res["test"]["status"] == "BLOCKED_BY_POLICY"
    assert res["policy"]["allowed"] is False
    assert "Maximum desired task count reached" in res["policy"]["reason"]
    # Step functions must NOT have been called
    workflow.start_recovery.assert_not_called()


def test_dry_run_mode_never_modifies_ecs(mock_chaos_setup):
    """7. Dry-run mode generates anomaly & incident, evaluates policy, but never executes AWS modifications."""
    manager = mock_chaos_setup["manager"]
    workflow = mock_chaos_setup["workflow"]

    res = manager.run_cpu_spike(dry_run=True)

    assert res["mode"] == "DRY_RUN"
    assert res["test"]["status"] == "SIMULATED"
    assert res["policy"]["allowed"] is True
    assert res["planned_action"] == "SCALE_OUT"
    assert res["execution"] == "NOT_STARTED"
    # Step Functions must NOT have been started
    workflow.start_recovery.assert_not_called()


def test_chaos_test_generates_audit_events(mock_chaos_setup):
    """8. Chaos tests generate structured audit events in audit log."""
    manager = mock_chaos_setup["manager"]
    audit = manager.audit
    initial_log_count = len(audit.get_logs())

    manager.run_cpu_spike(dry_run=False)

    logs = audit.get_logs()
    assert len(logs) > initial_log_count
    events = [l.get("event") for l in logs]
    assert "CHAOS_TEST_STARTED" in events
    assert "CHAOS_INCIDENT_DETECTED" in events
    assert "CHAOS_POLICY_EVALUATED" in events
    assert "CHAOS_REMEDIATION_EXECUTED" in events
    assert "SELF_HEALING_VERIFIED" in events
    assert "CHAOS_TEST_COMPLETED" in events


def test_successful_recovery_reported_correctly(mock_chaos_setup):
    """9. Successful recovery is reported in test response and history."""
    manager = mock_chaos_setup["manager"]
    res = manager.run_cpu_spike(dry_run=False)

    assert res["recovery"]["recovered"] is True
    history = manager.get_history()
    assert len(history) > 0
    assert history[0]["status"] == "PASSED"
    assert history[0]["recovery_verified"] is True


def test_cooldown_safety_guardrail_blocks_consecutive_chaos_test(mock_chaos_setup):
    """Verify that triggering chaos twice consecutively triggers active cooldown policy block."""
    policy = RemediationPolicy(cooldown_seconds=120)
    manager = ChaosTestManager(
        policy=policy,
        workflow=mock_chaos_setup["workflow"],
        remediation=mock_chaos_setup["remediation"],
        recovery=mock_chaos_setup["recovery"],
    )

    # First run succeeds
    res1 = manager.run_cpu_spike(dry_run=False)
    assert res1["policy"]["allowed"] is True
    assert res1["test"]["status"] == "COMPLETED"

    # Immediate second run is blocked by cooldown
    res2 = manager.run_cpu_spike(dry_run=False)
    assert res2["test"]["status"] == "BLOCKED_BY_POLICY"
    assert res2["policy"]["allowed"] is False
    assert "Cooldown active" in res2["policy"]["reason"]
