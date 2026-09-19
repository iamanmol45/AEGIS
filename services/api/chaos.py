import time
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from detector import AegisDetector
from incident import IncidentManager
from correlation import IncidentCorrelationEngine
from policy import RemediationPolicy
from workflow import StepFunctionsWorkflowManager
from remediation import RemediationEngine
from recovery import RecoveryVerifier
from audit import AuditLogger


class ChaosTestManager:
    """
    AEGIS Controlled Fault Injection & Chaos Demo Suite.
    Provides safe, non-destructive failure simulation across CPU, Task Failure,
    Memory Pressure, and Multi-Signal scenarios with complete end-to-end auditability.
    """

    def __init__(
        self,
        policy: Optional[RemediationPolicy] = None,
        workflow: Optional[StepFunctionsWorkflowManager] = None,
        correlation_engine: Optional[IncidentCorrelationEngine] = None,
        incident_manager: Optional[IncidentManager] = None,
        detector: Optional[AegisDetector] = None,
        remediation: Optional[RemediationEngine] = None,
        recovery: Optional[RecoveryVerifier] = None,
        audit: Optional[AuditLogger] = None,
    ):
        self.policy = policy or RemediationPolicy()
        self.workflow = workflow or StepFunctionsWorkflowManager()
        self.correlation_engine = correlation_engine or IncidentCorrelationEngine(window_seconds=60)
        self.incident_manager = incident_manager or IncidentManager()
        self.detector = detector or AegisDetector()
        self.remediation = remediation or RemediationEngine(policy=self.policy)
        self.recovery = recovery or RecoveryVerifier()
        self.audit = audit or AuditLogger()
        self.history: List[Dict[str, Any]] = []

    def get_available_tests(self) -> List[str]:
        return [
            "CPU_SPIKE",
            "TASK_FAILURE",
            "MEMORY_PRESSURE",
            "MULTI_SIGNAL_INCIDENT",
        ]

    def get_history(self) -> List[Dict[str, Any]]:
        return self.history

    def _get_current_desired_count(self) -> int:
        try:
            service = self.remediation.ecs.describe_services(
                cluster=self.remediation.cluster,
                services=[self.remediation.service]
            )["services"][0]
            return service.get("desiredCount", 1)
        except Exception:
            return 1

    def run_cpu_spike(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 1: Controlled CPU Anomaly Simulation (95% CPU)."""
        test_id = f"CHAOS-CPU-{int(time.time())}"
        scenario = "CPU_SPIKE"
        now_iso = datetime.utcnow().isoformat()

        self.audit.log({
            "event": "CHAOS_TEST_STARTED",
            "test_id": test_id,
            "scenario": scenario,
            "dry_run": dry_run,
        })

        # 1. Detection Phase (Simulated Metric)
        metric_name = "cpu"
        simulated_value = 95.0
        detection = self.detector.check_metric(metric_name, simulated_value)

        # 2. Incident Creation
        incident = self.incident_manager.create_incident(
            metric=detection["metric"],
            value=detection["value"],
            threshold=detection["threshold"],
            severity=detection["severity"],
            metadata={"chaos_test_id": test_id, "scenario": scenario},
        )

        self.audit.log({
            "event": "CHAOS_INCIDENT_DETECTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "metric": incident.metric,
            "value": incident.value,
            "severity": incident.severity,
        })

        # 3. Policy Evaluation
        action = "SCALE_OUT"
        current_desired = self._get_current_desired_count()
        target_desired = current_desired + 1

        policy_decision = self.policy.evaluate(
            incident=incident.to_dict(),
            action=action,
            current_desired_count=current_desired,
        )

        self.audit.log({
            "event": "CHAOS_POLICY_EVALUATED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": policy_decision["allowed"],
            "policy_reason": policy_decision["reason"],
        })

        # 4. Dry Run Handling
        if dry_run:
            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "SIMULATED",
                "recovery_verified": False,
                "mode": "DRY_RUN",
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            self.audit.log({
                "event": "CHAOS_TEST_COMPLETED",
                "test_id": test_id,
                "scenario": scenario,
                "incident_id": incident.id,
                "action": action,
                "mode": "DRY_RUN",
                "policy_allowed": policy_decision["allowed"],
                "workflow_status": "SKIPPED_DRY_RUN",
                "recovery_verified": False,
            })

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "SIMULATED", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "planned_action": action,
                "target_desired_count": target_desired,
                "execution": "NOT_STARTED",
                "mode": "DRY_RUN",
            }

        # 5. Policy Gate Enforcement
        if not policy_decision["allowed"]:
            self.audit.log({
                "event": "CHAOS_REMEDIATION_BLOCKED",
                "test_id": test_id,
                "incident_id": incident.id,
                "action": action,
                "policy_allowed": False,
                "policy_reason": policy_decision["reason"],
            })

            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "BLOCKED_BY_POLICY",
                "recovery_verified": False,
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        # 6. Step Functions Remediation
        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        exec_info = self.workflow.start_recovery(
            incident=incident.to_dict(),
            action=action,
            target_desired_count=target_desired,
        )

        self.policy.record_action()

        workflow_result = self.workflow.wait_for_completion(
            exec_info["execution_arn"],
            timeout_seconds=120,
        )

        # 7. Recovery Verification
        recovery_result = self.recovery.verify(target_desired)

        self.audit.log({
            "event": "CHAOS_RECOVERY_VERIFIED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "recovery_verified": recovery_result.get("recovered", False),
            "desired_count": recovery_result.get("desired_count"),
            "running_count": recovery_result.get("running_count"),
        })

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": True,
            "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
            "recovery_verified": recovery_result.get("recovered", False),
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if recovery_result.get("recovered", False) else "FAILED",
            "recovery_verified": recovery_result.get("recovered", False),
            "timestamp": now_iso,
        }
        self.history.insert(0, test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }

    def run_task_failure(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 2: Controlled Task Failure Simulation (0 running tasks)."""
        test_id = f"CHAOS-TASK-{int(time.time())}"
        scenario = "TASK_FAILURE"
        now_iso = datetime.utcnow().isoformat()

        self.audit.log({
            "event": "CHAOS_TEST_STARTED",
            "test_id": test_id,
            "scenario": scenario,
            "dry_run": dry_run,
        })

        # 1. Incident Creation for Task Failure
        incident = self.incident_manager.create_incident(
            metric="task_failure",
            value=0.0,
            threshold=1.0,
            severity="CRITICAL",
            metadata={"chaos_test_id": test_id, "scenario": scenario},
        )

        self.audit.log({
            "event": "CHAOS_INCIDENT_DETECTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "metric": "task_failure",
            "value": 0.0,
            "severity": "CRITICAL",
        })

        # 2. Policy Evaluation
        action = "RESTART_TASKS"
        current_desired = self._get_current_desired_count()
        target_desired = current_desired if current_desired > 0 else 1

        policy_decision = self.policy.evaluate(
            incident=incident.to_dict(),
            action=action,
            current_desired_count=current_desired,
        )

        self.audit.log({
            "event": "CHAOS_POLICY_EVALUATED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": policy_decision["allowed"],
            "policy_reason": policy_decision["reason"],
        })

        if dry_run:
            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "SIMULATED",
                "recovery_verified": False,
                "mode": "DRY_RUN",
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            self.audit.log({
                "event": "CHAOS_TEST_COMPLETED",
                "test_id": test_id,
                "scenario": scenario,
                "incident_id": incident.id,
                "action": action,
                "mode": "DRY_RUN",
                "policy_allowed": policy_decision["allowed"],
                "workflow_status": "SKIPPED_DRY_RUN",
                "recovery_verified": False,
            })

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "SIMULATED", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "planned_action": action,
                "target_desired_count": target_desired,
                "execution": "NOT_STARTED",
                "mode": "DRY_RUN",
            }

        if not policy_decision["allowed"]:
            self.audit.log({
                "event": "CHAOS_REMEDIATION_BLOCKED",
                "test_id": test_id,
                "incident_id": incident.id,
                "action": action,
                "policy_allowed": False,
                "policy_reason": policy_decision["reason"],
            })

            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "BLOCKED_BY_POLICY",
                "recovery_verified": False,
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        exec_info = self.workflow.start_recovery(
            incident=incident.to_dict(),
            action=action,
            target_desired_count=target_desired,
        )

        self.policy.record_action()

        workflow_result = self.workflow.wait_for_completion(
            exec_info["execution_arn"],
            timeout_seconds=120,
        )

        recovery_result = self.recovery.verify(target_desired)

        self.audit.log({
            "event": "CHAOS_RECOVERY_VERIFIED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "recovery_verified": recovery_result.get("recovered", False),
        })

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": True,
            "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
            "recovery_verified": recovery_result.get("recovered", False),
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if recovery_result.get("recovered", False) else "FAILED",
            "recovery_verified": recovery_result.get("recovered", False),
            "timestamp": now_iso,
        }
        self.history.insert(0, test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }

    def run_memory_pressure(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 3: Controlled Memory Anomaly Simulation (95% Memory)."""
        test_id = f"CHAOS-MEM-{int(time.time())}"
        scenario = "MEMORY_PRESSURE"
        now_iso = datetime.utcnow().isoformat()

        self.audit.log({
            "event": "CHAOS_TEST_STARTED",
            "test_id": test_id,
            "scenario": scenario,
            "dry_run": dry_run,
        })

        metric_name = "memory"
        simulated_value = 95.0
        detection = self.detector.check_metric(metric_name, simulated_value)

        incident = self.incident_manager.create_incident(
            metric=detection["metric"],
            value=detection["value"],
            threshold=detection["threshold"],
            severity=detection["severity"],
            metadata={"chaos_test_id": test_id, "scenario": scenario},
        )

        self.audit.log({
            "event": "CHAOS_INCIDENT_DETECTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "metric": incident.metric,
            "value": incident.value,
            "severity": incident.severity,
        })

        action = "SCALE_OUT"
        current_desired = self._get_current_desired_count()
        target_desired = current_desired + 1

        policy_decision = self.policy.evaluate(
            incident=incident.to_dict(),
            action=action,
            current_desired_count=current_desired,
        )

        self.audit.log({
            "event": "CHAOS_POLICY_EVALUATED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": policy_decision["allowed"],
            "policy_reason": policy_decision["reason"],
        })

        if dry_run:
            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "SIMULATED",
                "recovery_verified": False,
                "mode": "DRY_RUN",
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            self.audit.log({
                "event": "CHAOS_TEST_COMPLETED",
                "test_id": test_id,
                "scenario": scenario,
                "incident_id": incident.id,
                "action": action,
                "mode": "DRY_RUN",
                "policy_allowed": policy_decision["allowed"],
                "workflow_status": "SKIPPED_DRY_RUN",
                "recovery_verified": False,
            })

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "SIMULATED", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "planned_action": action,
                "target_desired_count": target_desired,
                "execution": "NOT_STARTED",
                "mode": "DRY_RUN",
            }

        if not policy_decision["allowed"]:
            self.audit.log({
                "event": "CHAOS_REMEDIATION_BLOCKED",
                "test_id": test_id,
                "incident_id": incident.id,
                "action": action,
                "policy_allowed": False,
                "policy_reason": policy_decision["reason"],
            })

            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "BLOCKED_BY_POLICY",
                "recovery_verified": False,
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        exec_info = self.workflow.start_recovery(
            incident=incident.to_dict(),
            action=action,
            target_desired_count=target_desired,
        )

        self.policy.record_action()

        workflow_result = self.workflow.wait_for_completion(
            exec_info["execution_arn"],
            timeout_seconds=120,
        )

        recovery_result = self.recovery.verify(target_desired)

        self.audit.log({
            "event": "CHAOS_RECOVERY_VERIFIED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "recovery_verified": recovery_result.get("recovered", False),
        })

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": True,
            "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
            "recovery_verified": recovery_result.get("recovered", False),
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if recovery_result.get("recovered", False) else "FAILED",
            "recovery_verified": recovery_result.get("recovered", False),
            "timestamp": now_iso,
        }
        self.history.insert(0, test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }

    def run_multi_signal(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 4: Controlled Multi-Signal Compound Anomaly (CPU 95% + Memory 92% + Task Failure 0)."""
        test_id = f"CHAOS-MULTI-{int(time.time())}"
        scenario = "MULTI_SIGNAL_INCIDENT"
        now_iso = datetime.utcnow().isoformat()

        self.audit.log({
            "event": "CHAOS_TEST_STARTED",
            "test_id": test_id,
            "scenario": scenario,
            "dry_run": dry_run,
        })

        # Feed signals into correlation engine
        s1 = {"metric": "cpu", "value": 95.0, "threshold": 80.0, "severity": "CRITICAL", "timestamp": now_iso}
        s2 = {"metric": "memory", "value": 92.0, "threshold": 80.0, "severity": "CRITICAL", "timestamp": now_iso}
        s3 = {"metric": "task_failure", "value": 0.0, "threshold": 1.0, "severity": "CRITICAL", "timestamp": now_iso}

        self.correlation_engine.add_signal(s1)
        self.correlation_engine.add_signal(s2)
        self.correlation_engine.add_signal(s3)

        correlated = self.correlation_engine.correlate()
        if not correlated:
            # Fallback construct
            correlated = {
                "correlated": True,
                "incident_id": f"INC-CORR-{int(time.time())}",
                "signals": [s1, s2, s3],
                "signal_count": 3,
                "severity": "CRITICAL",
                "reason": "Multiple correlated infrastructure signals detected (cpu, memory, task_failure)",
                "timestamp": now_iso,
                "incident_type": "CORRELATED",
            }

        incident = self.incident_manager.create_correlated_incident(correlated)

        self.audit.log({
            "event": "CHAOS_INCIDENT_DETECTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "incident_type": "CORRELATED",
            "signal_count": incident.signal_count or 3,
            "severity": incident.severity,
            "reason": incident.reason,
        })

        # Priority remediation: task_failure requires RESTART_TASKS
        action = "RESTART_TASKS"
        current_desired = self._get_current_desired_count()
        target_desired = current_desired if current_desired > 0 else 1

        policy_decision = self.policy.evaluate(
            incident=incident.to_dict(),
            action=action,
            current_desired_count=current_desired,
        )

        self.audit.log({
            "event": "CHAOS_POLICY_EVALUATED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": policy_decision["allowed"],
            "policy_reason": policy_decision["reason"],
        })

        if dry_run:
            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "SIMULATED",
                "recovery_verified": False,
                "mode": "DRY_RUN",
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            self.audit.log({
                "event": "CHAOS_TEST_COMPLETED",
                "test_id": test_id,
                "scenario": scenario,
                "incident_id": incident.id,
                "action": action,
                "mode": "DRY_RUN",
                "policy_allowed": policy_decision["allowed"],
                "workflow_status": "SKIPPED_DRY_RUN",
                "recovery_verified": False,
            })

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "SIMULATED", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "planned_action": action,
                "target_desired_count": target_desired,
                "execution": "NOT_STARTED",
                "mode": "DRY_RUN",
            }

        if not policy_decision["allowed"]:
            self.audit.log({
                "event": "CHAOS_REMEDIATION_BLOCKED",
                "test_id": test_id,
                "incident_id": incident.id,
                "action": action,
                "policy_allowed": False,
                "policy_reason": policy_decision["reason"],
            })

            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "BLOCKED_BY_POLICY",
                "recovery_verified": False,
                "timestamp": now_iso,
            }
            self.history.insert(0, test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        exec_info = self.workflow.start_recovery(
            incident=incident.to_dict(),
            action=action,
            target_desired_count=target_desired,
        )

        self.policy.record_action()

        workflow_result = self.workflow.wait_for_completion(
            exec_info["execution_arn"],
            timeout_seconds=120,
        )

        recovery_result = self.recovery.verify(target_desired)

        self.audit.log({
            "event": "CHAOS_RECOVERY_VERIFIED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "recovery_verified": recovery_result.get("recovered", False),
        })

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": True,
            "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
            "recovery_verified": recovery_result.get("recovered", False),
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if recovery_result.get("recovered", False) else "FAILED",
            "recovery_verified": recovery_result.get("recovered", False),
            "timestamp": now_iso,
        }
        self.history.insert(0, test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }
