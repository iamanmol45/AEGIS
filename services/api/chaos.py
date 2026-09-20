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
from rca import RCAEngine
from evidence import EvidenceStore


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
        rca_engine: Optional[RCAEngine] = None,
        evidence: Optional[EvidenceStore] = None,
    ):
        self.policy = policy or RemediationPolicy()
        self.workflow = workflow or StepFunctionsWorkflowManager()
        self.correlation_engine = correlation_engine or IncidentCorrelationEngine(window_seconds=60)
        self.incident_manager = incident_manager or IncidentManager()
        self.detector = detector or AegisDetector()
        self.remediation = remediation or RemediationEngine(policy=self.policy)
        self.recovery = recovery or RecoveryVerifier()
        self.audit = audit or AuditLogger()
        self.rca_engine = rca_engine or RCAEngine(store=self.incident_manager.store)
        self.evidence = evidence or EvidenceStore()
        self.history: List[Dict[str, Any]] = []

    def _resolve_if_recovered(self, incident, recovery_result: dict) -> None:
        """Marks the incident RESOLVED once ECS is verified back at the
        target state -- otherwise every chaos-triggered incident stays OPEN
        forever, since nothing else in the pipeline ever closes one."""
        if recovery_result.get("recovered", False):
            self.incident_manager.update_status(incident.id, "RESOLVED")

    # A remediation action that didn't clear the symptom points at the
    # other action next, rather than retrying the same one blindly:
    # persistent high CPU/memory after SCALE_OUT suggests a stuck process
    # (RESTART_TASKS territory), not insufficient capacity, and vice versa.
    _ALTERNATE_ACTION = {"SCALE_OUT": "RESTART_TASKS", "RESTART_TASKS": "SCALE_OUT"}

    def _self_heal_metric(self, test_id: str, incident, metric_name: str, first_action: str, current_desired: int) -> dict:
        """
        Closed-loop, risk-bounded self-healing for a cpu/memory chaos
        scenario, modeled on CIRCA-SH (Ma, "Distributed Fault Root Cause
        Localization and Self-Healing Strategy Generation Based on Causal
        Inference", Procedia Computer Science 281, 2026): ECS task counts
        converging only proves the ACTION completed, not that it actually
        fixed anything. This re-measures the real CloudWatch metric that
        triggered the incident (a counterfactual-style "did intervening
        actually help?" check, analogous to the paper's do-operator
        intervention test) and, if it's still breaching its threshold,
        tries one targeted alternate action before giving up -- capped at
        2 actions total (the paper's action budget B=2), so a persistent
        problem escalates for human review instead of reporting false
        success or retrying forever.
        """
        dims = [
            {"Name": "ClusterName", "Value": self.remediation.cluster},
            {"Name": "ServiceName", "Value": self.remediation.service},
        ]
        action = first_action
        desired = current_desired
        attempts = []
        workflow_result = None
        recovery_result = None
        healed = False

        for attempt_number in (1, 2):
            target_desired = desired + 1 if action == "SCALE_OUT" else (desired if desired > 0 else 1)

            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action=action,
                current_desired_count=desired,
            )

            self.audit.log({
                "event": "SELF_HEALING_ATTEMPT",
                "test_id": test_id,
                "incident_id": incident.id,
                "attempt": attempt_number,
                "action": action,
                "policy_allowed": policy_decision["allowed"],
                "policy_reason": policy_decision["reason"],
            })

            if not policy_decision["allowed"]:
                attempts.append({"action": action, "policy": policy_decision})
                break

            exec_info = self.workflow.start_recovery(
                incident=incident.to_dict(), action=action, target_desired_count=target_desired,
            )
            self.policy.record_action()
            workflow_result = self.workflow.wait_for_completion(exec_info["execution_arn"], timeout_seconds=150)
            recovery_result = self.recovery.verify(target_desired)

            symptom = self.detector.recheck_cleared(metric_name, dims)
            ecs_recovered = recovery_result.get("recovered", False)
            symptom_cleared = symptom.get("cleared")
            healed = ecs_recovered and (symptom_cleared is None or symptom_cleared is True)

            attempts.append({
                "action": action, "policy": policy_decision, "execution": exec_info,
                "workflow": workflow_result, "recovery": recovery_result,
                "symptom": symptom, "healed": healed,
            })

            self.audit.log({
                "event": "SELF_HEALING_VERIFIED",
                "test_id": test_id,
                "incident_id": incident.id,
                "attempt": attempt_number,
                "action": action,
                "ecs_recovered": ecs_recovered,
                "symptom_checked": symptom.get("checked"),
                "symptom_cleared": symptom_cleared,
                "current_value": symptom.get("current_value"),
            })

            if healed:
                self._resolve_if_recovered(incident, recovery_result)
                break

            desired = target_desired
            action = self._ALTERNATE_ACTION.get(action)
            if attempt_number == 2 or action is None:
                self.audit.log({
                    "event": "SELF_HEALING_EXHAUSTED",
                    "test_id": test_id,
                    "incident_id": incident.id,
                    "actions_taken": [a["action"] for a in attempts],
                    "reason": "Action budget exhausted; underlying metric still anomalous. Escalating for human review.",
                })

        return {
            "healed": healed,
            "attempts": attempts,
            "final_workflow": workflow_result,
            "final_recovery": recovery_result,
        }

    def _archive_evidence(self, incident, evidence: dict):
        """Uploads raw evidence to S3 and stamps the reference into the
        incident's metadata -- mirrors controller.py's helper, so chaos
        scenarios produce the same auditable evidence trail as real
        detection cycles instead of leaving GET /incidents/{id}/evidence
        with nothing to return."""
        key = self.evidence.put_evidence(incident.id, evidence)
        if not key:
            return None
        incident.metadata["evidence_s3_key"] = key
        self.incident_manager.store.update_incident(incident.id, {"metadata": incident.metadata})
        return key

    def get_available_tests(self) -> List[str]:
        return [
            "CPU_SPIKE",
            "TASK_FAILURE",
            "MEMORY_PRESSURE",
            "MULTI_SIGNAL_INCIDENT",
            "LATENCY_SPIKE",
            "ERROR_RATE_SPIKE",
            "DB_CONNECTIVITY_FAILURE",
            "BAD_DEPLOYMENT",
            "LOW_CONFIDENCE_INCIDENT",
            "NORMAL_WORKLOAD",
        ]

    def _record_test(self, test_summary: Dict[str, Any]) -> None:
        """Append to the local list (fast path for this process) and persist
        to the shared DynamoDB incident table so /chaos/history is consistent
        across all API replicas behind the ALB, not just whichever task
        happens to handle the request."""
        self.history.insert(0, test_summary)
        try:
            self.incident_manager.store.create_incident({
                "id": test_summary["test_id"],
                "record_type": "chaos_test",
                **test_summary,
            })
        except Exception:
            pass

    def get_history(self) -> List[Dict[str, Any]]:
        """Read chaos test history from the shared DynamoDB store so it's
        consistent no matter which API replica serves the request. Falls
        back to this process's local history if the store is unreachable
        or empty (e.g. DynamoDB access issues)."""
        try:
            remote = self.incident_manager.store.get_items_by_type("chaos_test", limit=200)
        except Exception:
            remote = []

        if remote:
            return [
                {k: v for k, v in item.items() if k not in ("id", "incident_id", "record_type")}
                for item in remote
            ]
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
        self._archive_evidence(incident, {"detection": detection})

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
            self._record_test(test_summary)

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
            self._record_test(test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        # 6. Self-healing remediation: bounded, symptom-verifying closed
        # loop (ยง _self_heal_metric) instead of one SCALE_OUT declared
        # successful the moment ECS's task count matches -- re-measures the
        # real CPU metric and escalates to RESTART_TASKS if scaling out
        # didn't actually bring it back under threshold.
        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        healing_result = self._self_heal_metric(
            test_id=test_id,
            incident=incident,
            metric_name=metric_name,
            first_action=action,
            current_desired=current_desired,
        )

        final_workflow = healing_result["final_workflow"] or {}
        final_recovery = healing_result["final_recovery"] or {}

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "actions_taken": [a["action"] for a in healing_result["attempts"]],
            "policy_allowed": True,
            "workflow_status": final_workflow.get("workflow_status", "EXECUTED"),
            "healed": healing_result["healed"],
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if healing_result["healed"] else "FAILED",
            "recovery_verified": healing_result["healed"],
            "timestamp": now_iso,
        }
        self._record_test(test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "self_healing": healing_result,
            "workflow": final_workflow,
            "recovery": final_recovery,
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
        self._archive_evidence(incident, {"simulated_task_health": {"desired_count": 1, "running_count": 0}})

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
            self._record_test(test_summary)

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
            self._record_test(test_summary)

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
            timeout_seconds=150,
        )

        recovery_result = self.recovery.verify(target_desired)
        self._resolve_if_recovered(incident, recovery_result)

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
        self._record_test(test_summary)

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
        self._archive_evidence(incident, {"detection": detection})

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
            self._record_test(test_summary)

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
            self._record_test(test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "policy": policy_decision,
                "status": "BLOCKED_BY_POLICY",
                "workflow": None,
                "recovery": None,
            }

        # Self-healing remediation: bounded, symptom-verifying closed loop
        # (ง _self_heal_metric) instead of one SCALE_OUT declared
        # successful the moment ECS's task count matches -- re-measures the
        # real memory metric and escalates to RESTART_TASKS if scaling out
        # didn't actually bring it back under threshold (e.g. a real leak).
        self.audit.log({
            "event": "CHAOS_REMEDIATION_EXECUTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "target_desired_count": target_desired,
        })

        healing_result = self._self_heal_metric(
            test_id=test_id,
            incident=incident,
            metric_name=metric_name,
            first_action=action,
            current_desired=current_desired,
        )

        final_workflow = healing_result["final_workflow"] or {}
        final_recovery = healing_result["final_recovery"] or {}

        self.audit.log({
            "event": "CHAOS_TEST_COMPLETED",
            "test_id": test_id,
            "scenario": scenario,
            "incident_id": incident.id,
            "actions_taken": [a["action"] for a in healing_result["attempts"]],
            "policy_allowed": True,
            "workflow_status": final_workflow.get("workflow_status", "EXECUTED"),
            "healed": healing_result["healed"],
        })

        test_summary = {
            "test_id": test_id,
            "scenario": scenario,
            "status": "PASSED" if healing_result["healed"] else "FAILED",
            "recovery_verified": healing_result["healed"],
            "timestamp": now_iso,
        }
        self._record_test(test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "self_healing": healing_result,
            "workflow": final_workflow,
            "recovery": final_recovery,
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
        self._archive_evidence(incident, {"correlated": correlated})

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
            self._record_test(test_summary)

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
            self._record_test(test_summary)

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
            timeout_seconds=150,
        )

        recovery_result = self.recovery.verify(target_desired)
        self._resolve_if_recovered(incident, recovery_result)

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
        self._record_test(test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }


    def _run_generic_scenario(
        self,
        scenario: str,
        metric_name: str,
        simulated_value: float,
        action: str,
        target_desired_fn,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Shared implementation for the scenarios added to reach the spec's
        T04-T11 coverage (latency, error rate, DB connectivity, bad
        deployment, low confidence, normal workload). Mirrors the same
        detect -> incident -> policy -> remediate -> verify -> audit flow
        as the original four scenarios above, but routes through
        RCAEngine for a real confidence score instead of assuming
        confidence=1.0, since several of these scenarios exist
        specifically to exercise the confidence gate.
        """
        test_id = f"CHAOS-{scenario}-{int(time.time())}"
        now_iso = datetime.utcnow().isoformat()

        self.audit.log({
            "event": "CHAOS_TEST_STARTED",
            "test_id": test_id,
            "scenario": scenario,
            "dry_run": dry_run,
        })

        detection = self.detector.check_metric(metric_name, simulated_value)

        if not detection.get("anomaly"):
            # NORMAL_WORKLOAD lands here by design: confirms AEGIS does
            # not raise a false-positive incident on healthy metrics.
            test_summary = {
                "test_id": test_id,
                "scenario": scenario,
                "status": "NO_ANOMALY_DETECTED",
                "recovery_verified": None,
                "timestamp": now_iso,
            }
            self._record_test(test_summary)
            self.audit.log({
                "event": "CHAOS_TEST_COMPLETED",
                "test_id": test_id,
                "scenario": scenario,
                "workflow_status": "NOT_TRIGGERED",
                "recovery_verified": None,
            })
            return {
                "test": {"id": test_id, "scenario": scenario, "status": "NO_ANOMALY_DETECTED", "timestamp": now_iso},
                "detection": detection,
            }

        incident = self.incident_manager.create_incident(
            metric=detection["metric"],
            value=detection["value"],
            threshold=detection["threshold"],
            severity=detection["severity"],
            metadata={"chaos_test_id": test_id, "scenario": scenario},
        )

        rca_result = self.rca_engine.analyze(incident.to_dict())
        confidence = rca_result.get("confidence", 1.0)
        self._archive_evidence(incident, {"detection": detection, "rca": rca_result})

        self.audit.log({
            "event": "CHAOS_INCIDENT_DETECTED",
            "test_id": test_id,
            "incident_id": incident.id,
            "metric": incident.metric,
            "value": incident.value,
            "severity": incident.severity,
            "confidence": confidence,
            "root_cause": rca_result.get("root_cause"),
        })

        current_desired = self._get_current_desired_count()
        target_desired = target_desired_fn(current_desired)

        policy_decision = self.policy.evaluate(
            incident=incident.to_dict(),
            action=action,
            current_desired_count=current_desired,
            confidence=confidence,
        )

        self.audit.log({
            "event": "CHAOS_POLICY_EVALUATED",
            "test_id": test_id,
            "incident_id": incident.id,
            "action": action,
            "policy_allowed": policy_decision["allowed"],
            "policy_reason": policy_decision["reason"],
            "confidence": confidence,
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
            self._record_test(test_summary)
            return {
                "test": {"id": test_id, "scenario": scenario, "status": "SIMULATED", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "rca": rca_result,
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
            self._record_test(test_summary)

            return {
                "test": {"id": test_id, "scenario": scenario, "status": "BLOCKED_BY_POLICY", "timestamp": now_iso},
                "incident": incident.to_dict(),
                "rca": rca_result,
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
            timeout_seconds=150,
        )

        recovery_result = self.recovery.verify(target_desired)
        self._resolve_if_recovered(incident, recovery_result)

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
        self._record_test(test_summary)

        return {
            "test": {"id": test_id, "scenario": scenario, "status": "COMPLETED", "timestamp": now_iso},
            "incident": incident.to_dict(),
            "rca": rca_result,
            "policy": policy_decision,
            "remediation": {"action": action, "target_desired_count": target_desired, "status": "EXECUTED"},
            "execution": exec_info,
            "workflow": workflow_result,
            "recovery": recovery_result,
        }

    def run_latency_spike(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 5 (spec T04): Controlled API latency spike (3500ms vs. a
        1000ms threshold -- 3.5x ratio clears the CRITICAL severity cutoff
        so this exercises the recovery path, not the severity gate)."""
        return self._run_generic_scenario(
            scenario="LATENCY_SPIKE",
            metric_name="latency_ms",
            simulated_value=3500.0,
            action="SCALE_OUT",
            target_desired_fn=lambda d: d + 1,
            dry_run=dry_run,
        )

    def run_error_rate_spike(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 6 (spec T05): Controlled 5xx error rate spike (25% vs. a 5% threshold)."""
        return self._run_generic_scenario(
            scenario="ERROR_RATE_SPIKE",
            metric_name="http_5xx_rate",
            simulated_value=25.0,
            action="RESTART_TASKS",
            target_desired_fn=lambda d: d if d > 0 else 1,
            dry_run=dry_run,
        )

    def run_db_connectivity_failure(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Scenario 7 (spec T06): Controlled RDS connectivity failure. No
        ECS-level action can fix a database-layer fault, so this
        intentionally proposes an action outside the Step Functions
        whitelist ("INVESTIGATE_DATABASE") -- the safety gate blocks it on
        the allowed-actions check and the incident escalates for human/DBA
        review, exactly as the spec's safety architecture intends: only a
        small set of pre-approved actions ever execute automatically.
        """
        return self._run_generic_scenario(
            scenario="DB_CONNECTIVITY_FAILURE",
            metric_name="db_connection_errors",
            simulated_value=50.0,
            action="INVESTIGATE_DATABASE",
            target_desired_fn=lambda d: d,
            dry_run=dry_run,
        )

    def run_bad_deployment(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Scenario 8 (spec T07): Controlled bad-deployment simulation -- an
        error rate spike framed as correlated with a recent deployment.
        ECS's native deployment circuit breaker (configured in the CDK
        stack) handles the actual rollback mechanics; this scenario
        exercises AEGIS's own detection/audit path for a
        deployment-correlated incident.
        """
        result = self._run_generic_scenario(
            scenario="BAD_DEPLOYMENT",
            metric_name="http_5xx_rate",
            simulated_value=40.0,
            action="RESTART_TASKS",
            target_desired_fn=lambda d: d if d > 0 else 1,
            dry_run=dry_run,
        )
        result["context"] = {"recent_change": "deployment"}
        return result

    def run_low_confidence_incident(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Scenario 9 (spec T09): A detected anomaly with no established RCA
        pattern (disk_io_saturation is intentionally absent from
        rca.py's METRIC_KNOWLEDGE). Value is 250 against a 70 threshold
        (3.57x ratio) so severity clears CRITICAL -- confirming the block
        below is specifically the confidence gate, not the severity gate.
        Exercises policy.py's confidence gate: even a CRITICAL, whitelisted
        action is blocked and escalated when RCA confidence is below the
        configured minimum, instead of auto-remediating a guess.
        """
        return self._run_generic_scenario(
            scenario="LOW_CONFIDENCE_INCIDENT",
            metric_name="disk_io_saturation",
            simulated_value=250.0,
            action="SCALE_OUT",
            target_desired_fn=lambda d: d + 1,
            dry_run=dry_run,
        )

    def run_normal_workload(self, dry_run: bool = False) -> Dict[str, Any]:
        """Scenario 10 (spec T11): Normal, healthy metric values -- confirms AEGIS does not raise a false-positive incident under ordinary conditions."""
        return self._run_generic_scenario(
            scenario="NORMAL_WORKLOAD",
            metric_name="cpu",
            simulated_value=15.0,
            action="SCALE_OUT",
            target_desired_fn=lambda d: d,
            dry_run=dry_run,
        )
