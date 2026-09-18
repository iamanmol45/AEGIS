from detector import AegisDetector
from incident import IncidentManager
from remediation import RemediationEngine
from recovery import RecoveryVerifier
from audit import AuditLogger
from policy import RemediationPolicy
from workflow import StepFunctionsWorkflowManager
import time


class AegisController:

    def __init__(self, policy: RemediationPolicy = None, workflow: StepFunctionsWorkflowManager = None):
        self.policy = policy or RemediationPolicy()
        self.detector = AegisDetector()
        self.incident_manager = IncidentManager()
        self.remediation = RemediationEngine(policy=self.policy)
        self.recovery = RecoveryVerifier()
        self.audit = AuditLogger()
        self.workflow = workflow or StepFunctionsWorkflowManager()

    def run(self):

        # 1. Check ECS task health
        ecs = self.remediation.ecs

        service = ecs.describe_services(
            cluster=self.remediation.cluster,
            services=[self.remediation.service]
        )["services"][0]

        desired = service["desiredCount"]
        running = service["runningCount"]

        # Task failure detected
        if desired > 0 and running == 0:
            incident = self.incident_manager.create_incident(
                metric="task_failure",
                value=running,
                threshold=desired,
                severity="CRITICAL",
            )

            # Evaluate Policy
            policy_decision = self.policy.evaluate(
                incident=incident,
                action="RESTART_TASKS",
                current_desired_count=desired
            )

            if not policy_decision["allowed"]:
                self.audit.log({
                    "event": "STEP_FUNCTION_RECOVERY",
                    "incident_id": incident["id"],
                    "action": "RESTART_TASKS",
                    "severity": incident["severity"],
                    "policy_allowed": False,
                    "policy_reason": policy_decision["reason"],
                    "execution_arn": None,
                    "workflow_status": "BLOCKED_BY_POLICY",
                    "recovery_verified": False,
                })

                return {
                    "anomaly": True,
                    "incident": incident,
                    "policy": policy_decision,
                    "workflow": None,
                    "status": "BLOCKED_BY_POLICY",
                }

            # Start Step Functions Workflow
            exec_info = self.workflow.start_recovery(
                incident=incident,
                action="RESTART_TASKS",
                target_desired_count=desired
            )

            self.policy.record_action()

            # Poll for workflow completion
            workflow_result = self.workflow.wait_for_completion(
                exec_info["execution_arn"],
                timeout_seconds=120
            )

            self.audit.log({
                "event": "STEP_FUNCTION_RECOVERY",
                "incident_id": incident["id"],
                "action": "RESTART_TASKS",
                "severity": incident["severity"],
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "execution_arn": exec_info["execution_arn"],
                "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
                "recovery_verified": workflow_result.get("recovery_verified", False),
            })

            return {
                "anomaly": True,
                "incident": incident,
                "policy": policy_decision,
                "execution": exec_info,
                "workflow": workflow_result,
            }

        # 2. Check Metrics (CPU / Memory)
        metrics = [
            ("cpu", "CPUUtilization"),
            ("memory", "MemoryUtilization"),
        ]

        results = []

        for metric_name, cloudwatch_metric in metrics:

            value = self.detector.get_latest_metric(
                metric_name=cloudwatch_metric,
                namespace="AWS/ECS",
                dimensions=[
                    {
                        "Name": "ClusterName",
                        "Value": "aegis-cluster",
                    },
                    {
                        "Name": "ServiceName",
                        "Value": "AegisInfrastructureStack-AegisApiServiceCEE6438E-NeHqjB9uxdtT",
                    },
                ],
            )

            if value is None:
                continue

            detection = self.detector.check_metric(
                metric_name,
                value,
            )

            results.append(detection)

            if not detection.get("anomaly"):
                continue

            incident = self.incident_manager.create_incident(
                metric=detection["metric"],
                value=detection["value"],
                threshold=detection["threshold"],
                severity=detection["severity"],
            )

            # Evaluate Policy for SCALE_OUT
            target_desired = desired + 1
            policy_decision = self.policy.evaluate(
                incident=incident,
                action="SCALE_OUT",
                current_desired_count=desired
            )

            if not policy_decision["allowed"]:
                self.audit.log({
                    "event": "STEP_FUNCTION_RECOVERY",
                    "incident_id": incident["id"],
                    "action": "SCALE_OUT",
                    "severity": incident["severity"],
                    "metric": incident["metric"],
                    "value": incident["value"],
                    "policy_allowed": False,
                    "policy_reason": policy_decision["reason"],
                    "execution_arn": None,
                    "workflow_status": "BLOCKED_BY_POLICY",
                    "recovery_verified": False,
                })

                return {
                    "anomaly": True,
                    "incident": incident,
                    "policy": policy_decision,
                    "workflow": None,
                    "status": "BLOCKED_BY_POLICY",
                }

            # Start Step Functions Workflow
            exec_info = self.workflow.start_recovery(
                incident=incident,
                action="SCALE_OUT",
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
                "incident_id": incident["id"],
                "action": "SCALE_OUT",
                "severity": incident["severity"],
                "metric": incident["metric"],
                "value": incident["value"],
                "policy_allowed": True,
                "policy_reason": policy_decision["reason"],
                "execution_arn": exec_info["execution_arn"],
                "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
                "recovery_verified": workflow_result.get("recovery_verified", False),
            })

            return {
                "anomaly": True,
                "incident": incident,
                "policy": policy_decision,
                "execution": exec_info,
                "workflow": workflow_result,
            }

        return {
            "anomaly": False,
            "results": results,
        }