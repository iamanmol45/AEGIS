from datetime import datetime


class AIReasoningEngine:

    def analyze(self, incident: dict, rca: dict):

        metric = incident.get("metric", "unknown")
        value = incident.get("value", 0)
        threshold = incident.get("threshold", 0)
        severity = incident.get("severity", "MEDIUM")
        service = incident.get("service", "aegis-api")

        if metric == "cpu":
            impact = (
                "The service may experience increased latency, "
                "reduced throughput, or request failures."
            )

            if value >= 90:
                likely_cause = (
                    "The ECS task is experiencing unusually high "
                    "CPU consumption, potentially due to a "
                    "CPU-intensive workload."
                )
            else:
                likely_cause = (
                    "CPU utilization has exceeded the configured "
                    "operational threshold."
                )

        elif metric == "memory":
            impact = (
                "The service may experience degraded performance "
                "or container instability."
            )

            if value >= 90:
                likely_cause = (
                    "The ECS task is consuming unusually high "
                    "memory, potentially due to a memory-intensive workload."
                )
            else:
                likely_cause = (
                    "Memory utilization has exceeded the configured threshold."
                )

        else:
            impact = "The affected service may experience degraded performance."
            likely_cause = f"An abnormal {metric} measurement was detected."

        recommendation = rca.get(
            "recommendation",
            "Investigate the affected service and metric.",
        )

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "incident_id": incident.get("id", "UNKNOWN"),
            "analysis": {
                "summary": (
                    f"{severity} {metric} anomaly detected in "
                    f"{service}."
                ),
                "likely_cause": likely_cause,
                "impact": impact,
                "evidence": {
                    "metric": metric,
                    "observed_value": value,
                    "threshold": threshold,
                },
                "recommended_action": recommendation,
            },
        }