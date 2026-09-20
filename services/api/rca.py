from datetime import datetime
from typing import Optional
from incident_store import IncidentStore

# Static service dependency graph for the AEGIS-protected application.
# Kept as data rather than a live discovery mechanism -- for a
# two-service app this is simpler and more reliable than building a
# dependency-crawler, and it's trivial to extend as services are added.
SERVICE_DEPENDENCIES = {
    "aegis-api-service": ["RDS PostgreSQL (aegis)"],
}

# One entry per detectable metric: how to describe it and a base
# confidence for a rule that has an established, well-understood pattern.
# Deliberately conservative -- multi-signal correlation and precedent
# match (below) are what push confidence higher, not the base rule alone.
METRIC_KNOWLEDGE = {
    "cpu": {
        "cause_high": "The ECS task is experiencing sustained high CPU utilization, likely from request load or a CPU-intensive workload.",
        "cause_threshold": "CPU utilization exceeded the configured operational threshold.",
        "recommendation": "Scale out ECS tasks to distribute load; investigate CPU-intensive request patterns if it recurs.",
        "base_confidence": 0.75,
    },
    "memory": {
        "cause_high": "The ECS task is experiencing sustained high memory utilization, consistent with a memory leak or a memory-intensive workload.",
        "cause_threshold": "Memory utilization exceeded the configured operational threshold.",
        "recommendation": "Scale out ECS tasks; if this recurs, investigate for a memory leak rather than relying on scaling alone.",
        "base_confidence": 0.70,
    },
    "task_failure": {
        "cause_high": "ECS tasks have crashed or been terminated and are not being replaced automatically at the expected rate.",
        "cause_threshold": "Running task count fell below the desired count.",
        "recommendation": "Restart ECS tasks; inspect container logs for the crash reason if this recurs.",
        "base_confidence": 0.85,
    },
    "latency_ms": {
        "cause_high": "Request latency has degraded sharply, consistent with resource saturation or a slow downstream dependency.",
        "cause_threshold": "Response time exceeded the configured latency threshold.",
        "recommendation": "Scale out ECS tasks to relieve load; check RDS query latency if it recurs after scaling.",
        "base_confidence": 0.65,
    },
    "http_5xx_rate": {
        "cause_high": "Server-side error rate has spiked sharply, consistent with a crashed dependency or a bad release.",
        "cause_threshold": "5xx error rate exceeded the configured threshold.",
        "recommendation": "Restart ECS tasks to clear a potential bad process state; check recent deployments if it recurs.",
        "base_confidence": 0.70,
    },
    "db_connection_errors": {
        "cause_high": "The application is failing to connect to RDS PostgreSQL, consistent with a database-layer outage or exhausted connection pool.",
        "cause_threshold": "Database connection error count exceeded the configured threshold.",
        "recommendation": "Escalate to on-call for database investigation; no automated ECS-level action can resolve a database-layer fault.",
        "base_confidence": 0.80,
    },
}

RESOLVED_STATUSES = {"RESOLVED", "RECOVERED", "CLOSED"}


class RCAEngine:
    """
    Deterministic root-cause analysis: combines the detected metric,
    correlated signal count, service dependency impact, and precedent from
    past resolved incidents into a probable cause with a numeric
    confidence score (spec FR-06 Root Cause Analysis / FR-07 Confidence
    Scoring). Kept behind this same analyze(incident) -> dict interface
    that a Bedrock-backed engine will use once account-level model access
    is approved (see docs/ARCHITECTURE_V2.md) -- swapping the
    implementation won't require touching controller.py or policy.py.
    """

    def __init__(self, store: Optional[IncidentStore] = None):
        self.store = store or IncidentStore()

    def _dependency_impact(self, service: str) -> list:
        return SERVICE_DEPENDENCIES.get(service, [])

    def _find_precedent(self, metric: str, service: str, exclude_incident_id: Optional[str] = None) -> Optional[dict]:
        """Looks for the most recent past incident on the same metric and
        service that reached a terminal, resolved state -- grounds the
        recommendation in what actually worked before, rather than a
        recommendation made in a vacuum."""
        try:
            past = self.store.get_incidents(limit=50)
        except Exception:
            return None

        for record in past:
            record_id = record.get("incident_id") or record.get("id")
            if record_id == exclude_incident_id:
                continue
            if metric not in record.get("metric", ""):
                continue
            if record.get("service", "aegis-api-service") != service:
                continue
            if str(record.get("status", "")).upper() in RESOLVED_STATUSES:
                return record
        return None

    def analyze(self, incident: dict) -> dict:
        metric_key = incident.get("metric", "unknown")
        # Correlated incidents use composite names like
        # "correlated_cpu_memory" -- match against the known base metrics.
        base_metric = next((m for m in METRIC_KNOWLEDGE if m in metric_key), None)
        value = incident.get("value", 0)
        threshold = incident.get("threshold", 0)
        severity = incident.get("severity", "MEDIUM")
        service = incident.get("service", "aegis-api-service")
        signal_count = incident.get("signal_count") or 1
        incident_id = incident.get("id") or incident.get("incident_id", "UNKNOWN")

        if base_metric:
            knowledge = METRIC_KNOWLEDGE[base_metric]
            severe_cutoff = threshold * 1.1 if threshold else 90
            cause = knowledge["cause_high"] if value >= severe_cutoff else knowledge["cause_threshold"]
            recommendation = knowledge["recommendation"]
            confidence = knowledge["base_confidence"]
        else:
            # No established pattern for this signal -- stay honest about
            # not knowing rather than guessing, so downstream policy
            # gating (min_confidence) can catch it and escalate instead
            # of auto-remediating a novel failure mode (spec FR-11 / T09).
            cause = f"Anomaly detected in {metric_key}; no established pattern for this signal."
            recommendation = "Investigate the affected service and metric manually."
            confidence = 0.35

        # Independent signals agreeing is stronger evidence than one
        # metric alone -- multi-signal correlation raises confidence.
        if signal_count > 1:
            confidence = min(0.97, confidence + 0.08 * (signal_count - 1))

        precedent = self._find_precedent(base_metric or metric_key, service, exclude_incident_id=incident_id)
        if precedent:
            precedent_id = precedent.get("incident_id") or precedent.get("id")
            confidence = min(0.98, confidence + 0.10)
            recommendation += (
                f" (Precedent: incident {precedent_id} with the same signature was previously resolved successfully.)"
            )
        else:
            precedent_id = None

        dependencies = self._dependency_impact(service)
        impact = (
            f"Downstream impact may extend to: {', '.join(dependencies)}."
            if dependencies else
            "No downstream dependencies mapped for this service."
        )

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "incident_id": incident_id,
            "severity": severity,
            "root_cause": cause,
            "confidence": round(confidence, 2),
            "evidence": {
                "metric": metric_key,
                "observed_value": value,
                "threshold": threshold,
                "signal_count": signal_count,
                "precedent_incident_id": precedent_id,
            },
            "impact": impact,
            "recommendation": recommendation,
        }
