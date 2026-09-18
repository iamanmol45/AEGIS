import boto3
from datetime import datetime, timedelta


class AegisDetector:

    def __init__(self):
        self.thresholds = {
            "cpu": 80.0,
            "memory": 80.0,
            "running_tasks": 1,
        }

        self.cloudwatch = boto3.client(
            "cloudwatch",
            region_name="ap-south-1",
        )

    def check_metric(self, metric_name, value):

        threshold = self.thresholds.get(metric_name)

        if threshold is None:
            return {
                "anomaly": False,
                "reason": "Unknown metric",
            }

        if metric_name == "running_tasks":
            abnormal = value < threshold
        else:
            abnormal = value > threshold

        if abnormal:
            return {
                "anomaly": True,
                "metric": metric_name,
                "value": value,
                "threshold": threshold,
                "timestamp": datetime.utcnow().isoformat(),
                "severity": self.get_severity(
                    metric_name,
                    value,
                ),
            }

        return {
            "anomaly": False,
            "metric": metric_name,
            "value": value,
            "threshold": threshold,
        }

    def get_severity(self, metric_name, value):

        if metric_name == "running_tasks":
            return "HIGH"

        if value >= 95:
            return "CRITICAL"

        if value >= 80:
            return "HIGH"

        return "MEDIUM"

    def get_latest_metric(
        self,
        metric_name,
        namespace,
        dimensions,
    ):

        now = datetime.utcnow()
        response = self.cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=now - timedelta(minutes=10),
            EndTime=now,
            Period=60,
            Statistics=["Average"],
        )

        datapoints = response.get("Datapoints", [])

        if not datapoints:
            return None

        datapoints.sort(
            key=lambda x: x["Timestamp"],
            reverse=True,
        )

        return datapoints[0]["Average"]