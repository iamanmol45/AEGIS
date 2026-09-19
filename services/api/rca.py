from datetime import datetime


class RCAEngine:

    def analyze(self, incident: dict):

        metric = incident.get("metric", "unknown")
        value = incident.get("value", 0)
        threshold = incident.get("threshold", 0)
        severity = incident.get("severity", "MEDIUM")

        if metric == "cpu":
            if value >= 90:
                cause = "High CPU utilization detected in the ECS service."
                recommendation = (
                    "Scale out ECS tasks and investigate CPU-intensive processes."
                )
            else:
                cause = "CPU utilization exceeded the configured threshold."
                recommendation = (
                    "Monitor CPU utilization and consider ECS auto-scaling."
                )

        elif metric == "memory":
            if value >= 90:
                cause = "High memory utilization detected in the ECS service."
                recommendation = (
                    "Scale out ECS tasks and investigate memory-consuming processes."
                )
            else:
                cause = "Memory utilization exceeded the configured threshold."
                recommendation = (
                    "Monitor memory usage and consider increasing task capacity."
                )

        else:
            cause = f"Anomaly detected in {metric}."
            recommendation = "Investigate the affected service and metric."

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "incident_id": incident.get("id", "UNKNOWN"),
            "severity": severity,
            "root_cause": cause,
            "evidence": {
                "metric": metric,
                "observed_value": value,
                "threshold": threshold,
            },
            "recommendation": recommendation,
        }
