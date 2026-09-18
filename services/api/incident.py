from datetime import datetime
from typing import Dict, List, Optional, Any
import uuid
from pydantic import BaseModel, Field
from incident_store import IncidentStore


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"INC-{uuid.uuid4().hex[:8].upper()}")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    service: str = "aegis-api"
    metric: str
    value: float
    threshold: float
    severity: str = "HIGH"
    status: str = "OPEN"
    description: str
    recommended_action: str
    incident_type: str = "SINGLE"  # "SINGLE" or "CORRELATED"
    signals: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    signal_count: Optional[int] = None
    reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    def __getitem__(self, item):
        return getattr(self, item)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class IncidentManager:
    """
    AEGIS Incident Manager backed by persistent DynamoDB IncidentStore.
    """

    def __init__(self, store: Optional[IncidentStore] = None):
        self.store = store or IncidentStore()
        self.incidents: List[Incident] = []

    def create_incident(
        self,
        metric: str,
        value: float,
        threshold: float,
        severity: str = "HIGH",
        service: str = "aegis-api",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Incident:
        action = self.get_recommended_action(metric, value)
        description = (
            f"Anomaly detected in {service}: {metric} measured at {value:.2f} "
            f"(threshold: {threshold:.2f}, severity: {severity})"
        )

        incident = Incident(
            service=service,
            metric=metric,
            value=value,
            threshold=threshold,
            severity=severity,
            status="OPEN",
            description=description,
            recommended_action=action,
            incident_type="SINGLE",
            metadata=metadata or {},
        )

        # Store in persistent store and in-memory list
        self.store.create_incident(incident.to_dict())
        self.incidents.insert(0, incident)
        return incident

    def create_correlated_incident(
        self,
        correlated_data: Dict[str, Any],
        service: str = "aegis-api",
    ) -> Incident:
        """
        Create and persist a unified incident from correlated multi-signal results.
        """
        incident_id = correlated_data.get("incident_id") or correlated_data.get("id") or f"INC-CORR-{uuid.uuid4().hex[:8].upper()}"
        signals = correlated_data.get("signals", [])
        signal_count = correlated_data.get("signal_count", len(signals))
        severity = correlated_data.get("severity", "CRITICAL")
        reason = correlated_data.get("reason", "Multiple correlated infrastructure signals detected")
        timestamp = correlated_data.get("timestamp", datetime.utcnow().isoformat())

        metrics = [s.get("metric", "unknown") for s in signals]
        metric_summary = "correlated_" + "_".join(metrics)
        max_val = max([float(s.get("value", 0.0)) for s in signals]) if signals else 0.0

        if "task_failure" in metrics:
            action = "CRITICAL: Multiple failures including task failure detected. Restart ECS tasks and inspect logs."
        else:
            action = "CRITICAL: Correlated high resource usage detected. Scale out ECS task desired count."

        description = f"Correlated Anomaly in {service}: {reason} across {signal_count} signals."

        incident = Incident(
            id=incident_id,
            timestamp=timestamp,
            service=service,
            metric=metric_summary,
            value=max_val,
            threshold=0.0,
            severity=severity,
            status="OPEN",
            description=description,
            recommended_action=action,
            incident_type="CORRELATED",
            signals=signals,
            signal_count=signal_count,
            reason=reason,
            metadata={"correlation_window_seconds": str(correlated_data.get("correlation_window_seconds", 60))},
        )

        # Store in persistent DynamoDB store
        self.store.create_incident(incident.to_dict())
        self.incidents.insert(0, incident)
        return incident

    def create_from_anomaly(
        self,
        anomaly_data: dict,
        service: str = "aegis-api",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Incident]:
        if not anomaly_data.get("anomaly", False):
            return None

        return self.create_incident(
            metric=anomaly_data.get("metric", "unknown"),
            value=anomaly_data.get("value", 0.0),
            threshold=anomaly_data.get("threshold", 0.0),
            severity=anomaly_data.get("severity", "HIGH"),
            service=service,
            metadata=metadata,
        )

    def get_all(self, status: Optional[str] = None) -> List[Incident]:
        """Retrieve incidents from persistent store, returning Incident model objects."""
        stored_items = self.store.get_incidents(status=status)
        models = []
        for item in stored_items:
            try:
                # Ensure compatibility with Incident model
                inc = Incident(
                    id=item.get("incident_id") or item.get("id"),
                    timestamp=item.get("timestamp", datetime.utcnow().isoformat()),
                    service=item.get("service", "aegis-api"),
                    metric=item.get("metric", "unknown"),
                    value=float(item.get("value", 0.0)),
                    threshold=float(item.get("threshold", 0.0)),
                    severity=item.get("severity", "HIGH"),
                    status=item.get("status", "OPEN"),
                    description=item.get("description", ""),
                    recommended_action=item.get("recommended_action", ""),
                    incident_type=item.get("incident_type", "SINGLE"),
                    signals=item.get("signals", []),
                    signal_count=item.get("signal_count"),
                    reason=item.get("reason"),
                    metadata=item.get("metadata", {}),
                )
                models.append(inc)
            except Exception:
                pass
        return models or self.incidents

    def get_incidents(self, status: Optional[str] = None) -> List[Incident]:
        return self.get_all(status)

    def get_by_id(self, incident_id: str) -> Optional[Incident]:
        item = self.store.get_incident(incident_id)
        if item:
            return Incident(
                id=item.get("incident_id") or item.get("id"),
                timestamp=item.get("timestamp", datetime.utcnow().isoformat()),
                service=item.get("service", "aegis-api"),
                metric=item.get("metric", "unknown"),
                value=float(item.get("value", 0.0)),
                threshold=float(item.get("threshold", 0.0)),
                severity=item.get("severity", "HIGH"),
                status=item.get("status", "OPEN"),
                description=item.get("description", ""),
                recommended_action=item.get("recommended_action", ""),
                incident_type=item.get("incident_type", "SINGLE"),
                signals=item.get("signals", []),
                signal_count=item.get("signal_count"),
                reason=item.get("reason"),
                metadata=item.get("metadata", {}),
            )
        for inc in self.incidents:
            if inc.id == incident_id:
                return inc
        return None

    def update_status(self, incident_id: str, new_status: str) -> Optional[Incident]:
        self.store.update_incident(incident_id, {"status": new_status.upper()})
        incident = self.get_by_id(incident_id)
        if incident:
            incident.status = new_status.upper()
        return incident

    def get_recommended_action(self, metric: str, value: float) -> str:
        if metric == "cpu":
            if value >= 95:
                return "CRITICAL: Immediately scale out ECS task desired count and investigate high-CPU processes."
            return "HIGH: Monitor ECS CPU utilization; trigger auto-scaling if sustained."
        elif metric == "memory":
            if value >= 95:
                return "CRITICAL: Memory threshold exceeded. Check for memory leaks and restart unhealthy tasks."
            return "HIGH: Monitor container memory consumption; scale memory allocations if required."
        elif metric in ("running_tasks", "task_failure"):
            return "CRITICAL: Running tasks dropped below minimum threshold. Inspect ECS task crash logs in CloudWatch."
        return "Investigate anomaly and inspect recent deployment changes."
