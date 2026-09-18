from datetime import datetime
from typing import Dict, List, Optional
import uuid
from pydantic import BaseModel, Field


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
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict)

    def __getitem__(self, item):
        return getattr(self, item)

    def get(self, key, default=None):
        return getattr(self, key, default)


class IncidentManager:

    def __init__(self):
        self.incidents: List[Incident] = []

    def create_incident(
        self,
        metric: str,
        value: float,
        threshold: float,
        severity: str = "HIGH",
        service: str = "aegis-api",
        metadata: Optional[Dict[str, str]] = None,
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
            metadata=metadata or {},
        )

        self.incidents.insert(0, incident)
        return incident

    def create_from_anomaly(
        self,
        anomaly_data: dict,
        service: str = "aegis-api",
        metadata: Optional[Dict[str, str]] = None,
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
        if status:
            return [inc for inc in self.incidents if inc.status.upper() == status.upper()]
        return self.incidents

    def get_incidents(self, status: Optional[str] = None) -> List[Incident]:
        return self.get_all(status)

    def get_by_id(self, incident_id: str) -> Optional[Incident]:
        for inc in self.incidents:
            if inc.id == incident_id:
                return inc
        return None

    def update_status(self, incident_id: str, new_status: str) -> Optional[Incident]:
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
        elif metric == "running_tasks":
            return "CRITICAL: Running tasks dropped below minimum threshold. Inspect ECS task crash logs in CloudWatch."
        return "Investigate anomaly and inspect recent deployment changes."
