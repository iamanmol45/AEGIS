import boto3
import os
import time


class RecoveryVerifier:

    def __init__(self):
        self.ecs = boto3.client(
            "ecs",
            region_name=os.getenv("AWS_REGION", "ap-south-1")
        )

        self.cluster = os.getenv(
            "ECS_CLUSTER",
            "aegis-cluster"
        )

        self._service = os.getenv("ECS_SERVICE")

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
            print(f"Error discovering ECS service in recovery: {e}")

        return "AegisInfrastructureStack-AegisApiServiceCEE6438E-3UL7oJqplNbq"

    def verify(self, expected_count):
        services = self.ecs.describe_services(
            cluster=self.cluster,
            services=[self.service]
        ).get("services", [])

        if not services:
            return {
                "desired_count": 0,
                "running_count": 0,
                "pending_count": 0,
                "recovered": False,
                "error": "Service not found",
            }

        service = services[0]
        return {
            "desired_count": service["desiredCount"],
            "running_count": service["runningCount"],
            "pending_count": service["pendingCount"],
            "recovered": (
                service["desiredCount"] == expected_count
                and service["runningCount"] == expected_count
                and service["pendingCount"] == 0
            )
        }