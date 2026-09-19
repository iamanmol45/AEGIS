import boto3
import os
from datetime import datetime
from policy import RemediationPolicy


class RemediationEngine:

    def __init__(self, policy: RemediationPolicy = None):
        self.ecs = boto3.client(
            "ecs",
            region_name=os.getenv("AWS_REGION", "ap-south-1")
        )

        self.cluster = os.getenv(
            "ECS_CLUSTER",
            "aegis-cluster"
        )

        self._service = os.getenv("ECS_SERVICE")
        self.policy = policy or RemediationPolicy()

    @property
    def service(self):
        if self._service:
            return self._service

        try:
            response = self.ecs.list_services(cluster=self.cluster)
            arns = response.get("serviceArns", [])
            if arns:
                self._service = arns[0].split("/")[-1]
                return self._service
        except Exception as e:
            print(f"Error discovering ECS service: {e}")

        return "AegisInfrastructureStack-AegisApiServiceCEE6438E-3UL7oJqplNbq"

    @property
    def max_desired_count(self):
        return self.policy.max_desired_count

    @property
    def cooldown_seconds(self):
        return self.policy.cooldown_seconds

    def scale_out(self, incident, execute=True):
        now = datetime.utcnow()

        services = self.ecs.describe_services(
            cluster=self.cluster,
            services=[self.service]
        ).get("services", [])

        current = services[0]["desiredCount"] if services else 1

        target = current + 1
        incident_id = incident.get("id") if hasattr(incident, "get") else incident["id"]

        # Safety Guardrail 1: Explicit Execute Flag Check
        if not execute:
            return {
                "timestamp": now.isoformat(),
                "incident_id": incident_id,
                "action": "SCALE_OUT",
                "previous_desired_count": current,
                "target_desired_count": target,
                "status": "BLOCKED_BY_SAFETY_POLICY",
                "policy": {
                    "allowed": False,
                    "reason": "Execution flag set to False."
                },
            }

        # Safety Guardrail 2: Policy Evaluation (Severity, Max Count, Cooldown)
        policy_decision = self.policy.evaluate(
            incident=incident,
            action="SCALE_OUT",
            current_desired_count=current
        )

        if not policy_decision["allowed"]:
            return {
                "timestamp": now.isoformat(),
                "incident_id": incident_id,
                "action": "SCALE_OUT",
                "previous_desired_count": current,
                "target_desired_count": target,
                "status": "BLOCKED_BY_POLICY",
                "policy": {
                    "allowed": False,
                    "reason": policy_decision["reason"]
                },
            }

        # Policy allowed -> Execute AWS ECS scale out
        self.ecs.update_service(
            cluster=self.cluster,
            service=self.service,
            desiredCount=target
        )

        self.policy.record_action()

        return {
            "timestamp": now.isoformat(),
            "incident_id": incident_id,
            "action": "SCALE_OUT",
            "previous_desired_count": current,
            "target_desired_count": target,
            "status": "EXECUTED",
            "policy": {
                "allowed": True,
                "reason": policy_decision["reason"]
            },
        }

    def restart_tasks(self, incident, execute=True):
        now = datetime.utcnow()

        services = self.ecs.describe_services(
            cluster=self.cluster,
            services=[self.service]
        ).get("services", [])

        desired = services[0]["desiredCount"] if services else 1
        incident_id = incident.get("id") if hasattr(incident, "get") else incident["id"]

        if not execute:
            return {
                "timestamp": now.isoformat(),
                "incident_id": incident_id,
                "action": "RESTART_TASKS",
                "desired_count": desired,
                "status": "BLOCKED_BY_SAFETY_POLICY",
                "policy": {
                    "allowed": False,
                    "reason": "Execution flag set to False."
                },
            }

        policy_decision = self.policy.evaluate(
            incident=incident,
            action="RESTART_TASKS",
            current_desired_count=desired
        )

        if not policy_decision["allowed"]:
            return {
                "timestamp": now.isoformat(),
                "incident_id": incident_id,
                "action": "RESTART_TASKS",
                "desired_count": desired,
                "status": "BLOCKED_BY_POLICY",
                "policy": {
                    "allowed": False,
                    "reason": policy_decision["reason"]
                },
            }

        # Policy allowed -> Execute AWS ECS restart tasks
        self.ecs.update_service(
            cluster=self.cluster,
            service=self.service,
            desiredCount=0
        )

        self.ecs.update_service(
            cluster=self.cluster,
            service=self.service,
            desiredCount=desired
        )

        self.policy.record_action()

        return {
            "timestamp": now.isoformat(),
            "incident_id": incident_id,
            "action": "RESTART_TASKS",
            "desired_count": desired,
            "status": "EXECUTED",
            "policy": {
                "allowed": True,
                "reason": policy_decision["reason"]
            },
        }