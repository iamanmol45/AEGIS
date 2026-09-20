from datetime import datetime
from detector import AegisDetector
from incident import IncidentManager
from remediation import RemediationEngine
from recovery import RecoveryVerifier
from audit import AuditLogger
from policy import RemediationPolicy
from workflow import StepFunctionsWorkflowManager
from correlation import IncidentCorrelationEngine
from evidence import EvidenceStore
from rca import RCAEngine


class AegisController:

    def __init__(
        self,
        policy: RemediationPolicy = None,
        workflow: StepFunctionsWorkflowManager = None,
        correlation_engine: IncidentCorrelationEngine = None,
        incident_manager: IncidentManager = None,
    ):
        self.policy = policy or RemediationPolicy()
        self.detector = AegisDetector()
        self.incident_manager = incident_manager or IncidentManager()
        self.remediation = RemediationEngine(policy=self.policy)
        self.recovery = RecoveryVerifier()
        self.audit = AuditLogger()
        self.workflow = workflow or StepFunctionsWorkflowManager()
        self.correlation_engine = correlation_engine or IncidentCorrelationEngine(window_seconds=60)
        self.evidence = EvidenceStore()
        self.rca_engine = RCAEngine(store=self.incident_manager.store)

    def _resolve_if_recovered(self, incident, workflow_result: dict) -> None:
        """Marks the incident RESOLVED once Step Functions verifies ECS is
        back at the target state -- otherwise every autonomously-recovered
        incident stays OPEN forever, since nothing else in the pipeline
        ever closes one."""
        if workflow_result.get("recovery_verified", False):
            self.incident_manager.update_status(incident.id, "RESOLVED")

    # A remediation action that didn't clear the symptom points at the
    # other action next, rather than retrying the same one blindly:
    # persistent high CPU/memory after SCALE_OUT suggests a stuck process
    # (RESTART_TASKS territory), not insufficient capacity, and vice versa.
    _ALTERNATE_ACTION = {"SCALE_OUT": "RESTART_TASKS", "RESTART_TASKS": "SCALE_OUT"}

    def _self_heal_metric(self, incident, metric_name: str, first_action: str, current_desired: int, confidence: float) -> dict:
        """
        Closed-loop, risk-bounded self-healing for a single cpu/memory
        anomaly, modeled on CIRCA-SH (Ma, "Distributed Fault Root Cause
        Localization and Self-Healing Strategy Generation Based on Causal
        Inference", Procedia Computer Science 281, 2026): don't declare an
        incident healed just because ECS's task count converged -- that
        only proves the ACTION completed, not that it fixed anything.
        Re-measure the real CloudWatch metric that triggered the incident
        (a counterfactual-style "did intervening actually help?" check,
        analogous to the paper's do-operator intervention test) and, if it's
        still breaching its threshold, try one targeted alternate action
        before giving up -- capped at 2 actions total (the paper's action
        budget B=2), so a stuck problem escalates for human review instead
        of retrying forever or reporting false success.
        """
        dims = [
            {"Name": "ClusterName", "Value": self.remediation.cluster},
            {"Name": "ServiceName", "Value": self.remediation.service},
        ]
        action = first_action
        desired = current_desired
        attempts = []
        workflow_result = None
        healed = False

        for attempt_number in (1, 2):
            target_desired = desired + 1 if action == "SCALE_OUT" else (desired if desired > 0 else 1)

            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action=action,
                current_desired_count=desired,
                confidence=confidence,
            )

            self.audit.log({
                "event": "SELF_HEALING_ATTEMPT",
                "incident_id": incident.id,
                "attempt": attempt_number,
                "action": action,
                "policy_allowed": policy_decision["allowed"],
                "policy_reason": policy_decision["reason"],
            })

            if not policy_decision["allowed"]:
                attempts.append({"action": action, "policy": policy_decision, "workflow": None, "symptom": None})
                break

            exec_info = self.workflow.start_recovery(
                incident=incident.to_dict(), action=action, target_desired_count=target_desired,
            )
            self.policy.record_action()
            workflow_result = self.workflow.wait_for_completion(exec_info["execution_arn"], timeout_seconds=150)

            symptom = self.detector.recheck_cleared(metric_name, dims)
            ecs_recovered = workflow_result.get("recovery_verified", False)
            # If the metric can't be measured right now (no CloudWatch data
            # yet), fall back to ECS-level verification alone rather than
            # blocking forever on an unmeasurable symptom.
            symptom_cleared = symptom.get("cleared")
            healed = ecs_recovered and (symptom_cleared is None or symptom_cleared is True)

            attempts.append({
                "action": action, "policy": policy_decision,
                "workflow": workflow_result, "symptom": symptom, "healed": healed,
            })

            self.audit.log({
                "event": "SELF_HEALING_VERIFIED",
                "incident_id": incident.id,
                "attempt": attempt_number,
                "action": action,
                "ecs_recovered": ecs_recovered,
                "symptom_checked": symptom.get("checked"),
                "symptom_cleared": symptom_cleared,
                "current_value": symptom.get("current_value"),
            })

            if healed:
                self.incident_manager.update_status(incident.id, "RESOLVED")
                break

            desired = target_desired
            action = self._ALTERNATE_ACTION.get(action)
            if attempt_number == 2 or action is None:
                self.audit.log({
                    "event": "SELF_HEALING_EXHAUSTED",
                    "incident_id": incident.id,
                    "actions_taken": [a["action"] for a in attempts],
                    "reason": "Action budget exhausted; underlying metric still anomalous. Escalating for human review.",
                })

        return {"healed": healed, "attempts": attempts, "final_workflow": workflow_result}

    def _archive_evidence(self, incident, evidence: dict):
        """Uploads raw evidence to S3 and stamps the reference into the
        incident's metadata so DynamoDB stays lean but the full context
        (signals, detection payload) stays auditable."""
        key = self.evidence.put_evidence(incident.id, evidence)
        if not key:
            return
        incident.metadata["evidence_s3_key"] = key
        self.incident_manager.store.update_incident(incident.id, {"metadata": incident.metadata})
        return key

    def run(self):
        """
        Execute one complete autonomous detection, multi-signal correlation,
        policy evaluation, Step Functions remediation, recovery verification, and audit cycle.
        """
        # 1. Check ECS task health
        ecs = self.remediation.ecs

        services = ecs.describe_services(
            cluster=self.remediation.cluster,
            services=[self.remediation.service]
        ).get("services", [])

        if not services:
            return {"status": "NO_SERVICE", "message": "No ECS service found"}

        service = services[0]
        desired = service["desiredCount"]
        running = service["runningCount"]

        task_failure_detected = (desired > 0 and running == 0)
        if task_failure_detected:
            self.correlation_engine.add_signal({
                "metric": "task_failure",
                "value": float(running),
                "threshold": float(desired),
                "severity": "CRITICAL",
                "detection_method": "STATIC_THRESHOLD",
                "timestamp": datetime.utcnow().isoformat(),
            })

        # 2. Check Metrics (CPU / Memory)
        metrics = [
            ("cpu", "CPUUtilization"),
            ("memory", "MemoryUtilization"),
        ]

        results = []
        metric_anomalies = []

        for metric_name, cloudwatch_metric in metrics:
            value = self.detector.get_latest_metric(
                metric_name=cloudwatch_metric,
                namespace="AWS/ECS",
                dimensions=[
                    {
                        "Name": "ClusterName",
                        "Value": self.remediation.cluster,
                    },
                    {
                        "Name": "ServiceName",
                        "Value": self.remediation.service,
                    },
                ],
            )

            if value is None:
                continue

            detection = self.detector.check_metric(metric_name, value)
            results.append(detection)

            if detection.get("anomaly"):
                metric_anomalies.append(detection)
                self.correlation_engine.add_signal({
                    "metric": detection["metric"],
                    "value": detection["value"],
                    "threshold": detection["threshold"],
                    "severity": detection["severity"],
                    "detection_method": detection.get("detection_method", "STATIC_THRESHOLD"),
                    "timestamp": detection.get("timestamp", datetime.utcnow().isoformat()),
                })

        # ----------------------------------------------------
        # 3. Multi-Signal Incident Correlation Layer
        # ----------------------------------------------------
        correlated = self.correlation_engine.correlate()

        if correlated is not None:
            # Create ONE unified incident from correlated signals
            incident = self.incident_manager.create_correlated_incident(correlated)
            rca_result = self.rca_engine.analyze(incident.to_dict())
            confidence = rca_result.get("confidence", 1.0)
            evidence_key = self._archive_evidence(incident, {
                "correlated": correlated,
                "task_health": {"desired_count": desired, "running_count": running},
                "rca": rca_result,
            })

            # Audit the correlated incident creation
            self.audit.log({
                "event": "CORRELATED_INCIDENT",
                "incident_id": incident.id,
                "severity": incident.severity,
                "signal_count": incident.signal_count or len(correlated.get("signals", [])),
                "signals": [s["metric"] for s in correlated.get("signals", [])],
                "reason": incident.reason or "Multiple correlated infrastructure signals detected",
                "evidence_s3_key": evidence_key,
                "root_cause": rca_result.get("root_cause"),
                "confidence": confidence,
            })

            # Determine remediation action based on signal composition
            signals_metrics = [s["metric"] for s in correlated.get("signals", [])]
            if "task_failure" in signals_metrics:
                action = "RESTART_TASKS"
                target_desired = desired if desired > 0 else 1
            else:
                action = "SCALE_OUT"
                target_desired = desired + 1

            # Evaluate Policy Safety Gate
            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action=action,
                current_desired_count=desired,
                confidence=confidence,
            )

            if not policy_decision["allowed"]:
                self.audit.log({
                    "event": "STEP_FUNCTION_RECOVERY",
                    "incident_id": incident.id,
                    "action": action,
                    "severity": incident.severity,
                    "policy_allowed": False,
                    "policy_reason": policy_decision["reason"],
                    "execution_arn": None,
                    "workflow_status": "BLOCKED_BY_POLICY",
                    "recovery_verified": False,
                    "correlated": True,
                    "signal_count": incident.signal_count,
                })

                return {
                    "anomaly": True,
                    "correlated": True,
                    "incident": incident,
                    "policy": policy_decision,
                    "workflow": None,
                    "status": "BLOCKED_BY_POLICY",
                }

            # Start Step Functions Workflow
            exec_info = self.workflow.start_recovery(
                incident=incident.to_dict(),
                action=action,
                target_desired_count=target_desired
            )

            self.policy.record_action()

            # Poll for workflow completion
            workflow_result = self.workflow.wait_for_completion(
                exec_info["execution_arn"],
                timeout_seconds=150
            )
            self._resolve_if_recovered(incident, workflow_result)

            self.audit.log({
                "event": "STEP_FUNCTION_RECOVERY",
                "incident_id": incident.id,
                "action": action,
                "severity": incident.severity,
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "execution_arn": exec_info["execution_arn"],
                "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
                "recovery_verified": workflow_result.get("recovery_verified", False),
                "correlated": True,
                "signal_count": incident.signal_count,
            })

            return {
                "anomaly": True,
                "correlated": True,
                "incident": incident,
                "policy": policy_decision,
                "execution": exec_info,
                "workflow": workflow_result,
            }

        # ----------------------------------------------------
        # 4. Fallback: Single Incident Processing (Preserving verified behavior)
        # ----------------------------------------------------
        if task_failure_detected:
            incident = self.incident_manager.create_incident(
                metric="task_failure",
                value=running,
                threshold=desired,
                severity="CRITICAL",
            )
            rca_result = self.rca_engine.analyze(incident.to_dict())
            confidence = rca_result.get("confidence", 1.0)
            self._archive_evidence(incident, {
                "task_health": {"desired_count": desired, "running_count": running},
                "rca": rca_result,
            })

            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action="RESTART_TASKS",
                current_desired_count=desired,
                confidence=confidence,
            )

            if not policy_decision["allowed"]:
                self.audit.log({
                    "event": "STEP_FUNCTION_RECOVERY",
                    "incident_id": incident.id,
                    "action": "RESTART_TASKS",
                    "severity": incident.severity,
                    "policy_allowed": False,
                    "policy_reason": policy_decision["reason"],
                    "execution_arn": None,
                    "workflow_status": "BLOCKED_BY_POLICY",
                    "recovery_verified": False,
                    "confidence": confidence,
                })

                return {
                    "anomaly": True,
                    "correlated": False,
                    "incident": incident,
                    "policy": policy_decision,
                    "workflow": None,
                    "status": "BLOCKED_BY_POLICY",
                }

            exec_info = self.workflow.start_recovery(
                incident=incident.to_dict(),
                action="RESTART_TASKS",
                target_desired_count=desired
            )

            self.policy.record_action()

            workflow_result = self.workflow.wait_for_completion(
                exec_info["execution_arn"],
                timeout_seconds=150
            )
            self._resolve_if_recovered(incident, workflow_result)

            self.audit.log({
                "event": "STEP_FUNCTION_RECOVERY",
                "incident_id": incident.id,
                "action": "RESTART_TASKS",
                "severity": incident.severity,
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "execution_arn": exec_info["execution_arn"],
                "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
                "recovery_verified": workflow_result.get("recovery_verified", False),
                "confidence": confidence,
            })

            return {
                "anomaly": True,
                "correlated": False,
                "incident": incident,
                "policy": policy_decision,
                "execution": exec_info,
                "workflow": workflow_result,
            }

        for detection in metric_anomalies:
            incident = self.incident_manager.create_incident(
                metric=detection["metric"],
                value=detection["value"],
                threshold=detection["threshold"],
                severity=detection["severity"],
            )
            rca_result = self.rca_engine.analyze(incident.to_dict())
            confidence = rca_result.get("confidence", 1.0)
            self._archive_evidence(incident, {"detection": detection, "rca": rca_result})

            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action="SCALE_OUT",
                current_desired_count=desired,
                confidence=confidence,
            )

            if not policy_decision["allowed"]:
                self.audit.log({
                    "event": "STEP_FUNCTION_RECOVERY",
                    "incident_id": incident.id,
                    "action": "SCALE_OUT",
                    "severity": incident.severity,
                    "metric": incident.metric,
                    "value": incident.value,
                    "policy_allowed": False,
                    "policy_reason": policy_decision["reason"],
                    "execution_arn": None,
                    "workflow_status": "BLOCKED_BY_POLICY",
                    "recovery_verified": False,
                    "confidence": confidence,
                })

                return {
                    "anomaly": True,
                    "correlated": False,
                    "incident": incident,
                    "policy": policy_decision,
                    "workflow": None,
                    "status": "BLOCKED_BY_POLICY",
                }

            # Policy cleared the first action -- hand off to the bounded,
            # symptom-verifying self-healing loop rather than firing one
            # SCALE_OUT and declaring victory the moment ECS's task count
            # matches: this re-measures the real cpu/memory metric that
            # triggered the incident and escalates to RESTART_TASKS if
            # scaling out didn't actually bring it back under threshold.
            self.audit.log({
                "event": "STEP_FUNCTION_RECOVERY",
                "incident_id": incident.id,
                "action": "SCALE_OUT",
                "severity": incident.severity,
                "metric": incident.metric,
                "value": incident.value,
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "confidence": confidence,
            })

            healing_result = self._self_heal_metric(
                incident=incident,
                metric_name=incident.metric,
                first_action="SCALE_OUT",
                current_desired=desired,
                confidence=confidence,
            )

            return {
                "anomaly": True,
                "correlated": False,
                "incident": incident,
                "policy": policy_decision,
                "self_healing": healing_result,
                "workflow": healing_result.get("final_workflow"),
            }

        return {
            "anomaly": False,
            "results": results,
        }