from datetime import datetime


class AIReasoningEngine:
    """
    Produces a human-readable narrative from RCAEngine's structured
    output. Kept as its own pipeline stage (matching the spec's "Bedrock
    Reasoning" step) so it can be swapped for an actual Bedrock call later
    -- once account-level model access is approved -- without touching
    RCAEngine, policy.py, or controller.py.
    """

    def analyze(self, incident: dict, rca: dict):
        severity = incident.get("severity", rca.get("severity", "MEDIUM"))
        metric = incident.get("metric", rca.get("evidence", {}).get("metric", "unknown"))
        service = incident.get("service", "aegis-api-service")
        confidence = rca.get("confidence", 0.5)

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "incident_id": incident.get("id", rca.get("incident_id", "UNKNOWN")),
            "analysis": {
                "summary": f"{severity} {metric} anomaly detected in {service} (confidence: {confidence:.0%}).",
                "likely_cause": rca.get("root_cause", "Unknown"),
                "impact": rca.get("impact", "Impact not assessed."),
                "confidence": confidence,
                "evidence": rca.get("evidence", {}),
                "recommended_action": rca.get("recommendation", "Investigate the affected service and metric."),
            },
        }
