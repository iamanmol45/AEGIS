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

        self.service = os.getenv(
            "ECS_SERVICE",
            "AegisInfrastructureStack-AegisApiServiceCEE6438E-NeHqjB9uxdtT"
        )

    def verify(self, expected_count):

        service = self.ecs.describe_services(
            cluster=self.cluster,
            services=[self.service]
        )["services"][0]

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