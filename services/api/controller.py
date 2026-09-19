from datetime import datetime
from detector import AegisDetector
from incident import IncidentManager
from remediation import RemediationEngine
from recovery import RecoveryVerifier
from audit import AuditLogger
from policy import RemediationPolicy
from workflow import StepFunctionsWorkflowManager
from correlation import IncidentCorrelationEngine


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

            # Audit the correlated incident creation
            self.audit.log({
                "event": "CORRELATED_INCIDENT",
                "incident_id": incident.id,
                "severity": incident.severity,
                "signal_count": incident.signal_count or len(correlated.get("signals", [])),
                "signals": [s["metric"] for s in correlated.get("signals", [])],
                "reason": incident.reason or "Multiple correlated infrastructure signals detected",
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
                current_desired_count=desired
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
                timeout_seconds=120
            )

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

            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action="RESTART_TASKS",
                current_desired_count=desired
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
                timeout_seconds=120
            )

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

            target_desired = desired + 1
            policy_decision = self.policy.evaluate(
                incident=incident.to_dict(),
                action="SCALE_OUT",
                current_desired_count=desired
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
                action="SCALE_OUT",
                target_desired_count=target_desired
            )

            self.policy.record_action()

            workflow_result = self.workflow.wait_for_completion(
                exec_info["execution_arn"],
                timeout_seconds=120
            )

            self.audit.log({
                "event": "STEP_FUNCTION_RECOVERY",
                "incident_id": incident.id,
                "action": "SCALE_OUT",
                "severity": incident.severity,
                "metric": incident.metric,
                "value": incident.value,
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "execution_arn": exec_info["execution_arn"],
                "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
                "recovery_verified": workflow_result.get("recovery_verified", False),
            })

            return {
                "anomaly": True,
                "correlated": False,
                "incident": incident,
                "policy": policy_decision,
                "execution": exec_info,
                "workflow": workflow_result,
            }

        return {
            "anomaly": False,
            "results": results,
        }