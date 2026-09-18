from datetime import datetime


class AegisDetector:

    def __init__(self):
        self.thresholds = {
            "cpu": 80.0,
            "memory": 80.0,
            "running_tasks": 1,
        }

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
                "severity": self.get_severity(metric_name, value),
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