from datetime import datetime
from typing import Dict, List, Optional, Any
import uuid


class IncidentCorrelationEngine:
    """
    AEGIS Multi-Signal Incident Correlation Engine.
    
    Maintains a short-lived in-memory collection of detected anomaly signals
    and correlates multiple related anomalous signals within a configurable time window
    into ONE unified incident.
    """

    def __init__(self, window_seconds: int = 60):
        self.window_seconds = window_seconds
        self.signals: List[Dict[str, Any]] = []
        self.correlated_incident_ids: List[str] = []

    def _parse_timestamp(self, ts_input: Any) -> datetime:
        if isinstance(ts_input, datetime):
            return ts_input
        if isinstance(ts_input, str):
            try:
                clean_ts = ts_input.replace("Z", "+00:00")
                return datetime.fromisoformat(clean_ts).replace(tzinfo=None)
            except Exception:
                pass
        return datetime.utcnow()

    def add_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and store an anomaly signal.
        Expected keys: metric, value, threshold, severity, timestamp, optional detection_method.
        """
        if not isinstance(signal, dict):
            raise ValueError("Signal must be a dictionary")

        metric = str(signal.get("metric", "unknown")).lower()
        if metric == "running_tasks":
            metric = "task_failure"

        raw_ts = signal.get("timestamp")
        parsed_dt = self._parse_timestamp(raw_ts) if raw_ts else datetime.utcnow()

        stored_signal = {
            "id": signal.get("id", f"SIG-{uuid.uuid4().hex[:6].upper()}"),
            "metric": metric,
            "value": float(signal.get("value", 0.0)),
            "threshold": float(signal.get("threshold", 0.0)),
            "severity": str(signal.get("severity", "HIGH")).upper(),
            "timestamp": parsed_dt.isoformat(),
            "detection_method": signal.get("detection_method", "STATIC_THRESHOLD"),
            "consumed": False,
            "_parsed_dt": parsed_dt,
        }

        self.signals.append(stored_signal)
        return stored_signal

    def clear_expired(self, now: Optional[datetime] = None) -> int:
        """
        Remove signals older than the correlation window or already consumed.
        Returns the number of signals cleared.
        """
        current_time = now or datetime.utcnow()
        initial_count = len(self.signals)

        active_signals = []
        for s in self.signals:
            if s.get("consumed", False):
                continue
            signal_time = s.get("_parsed_dt", current_time)
            age_seconds = (current_time - signal_time).total_seconds()
            if 0 <= age_seconds <= self.window_seconds:
                active_signals.append(s)

        self.signals = active_signals
        return initial_count - len(self.signals)

    def correlate(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """
        Evaluate pending signals within the correlation window.
        - If fewer than 2 related anomalous signals exist: return None.
        - If 2 or more related anomalous signals exist: return ONE unified correlated incident.
        """
        current_time = now or datetime.utcnow()
        self.clear_expired(now=current_time)

        related_metrics = {"cpu", "memory", "task_failure"}
        pending = [
            s for s in self.signals
            if not s.get("consumed", False) and s["metric"] in related_metrics
        ]

        if len(pending) < 2:
            return None

        # Severity Hierarchy: CRITICAL > HIGH > MEDIUM > LOW
        severities = [s["severity"] for s in pending]
        if "CRITICAL" in severities:
            overall_severity = "CRITICAL"
        elif "HIGH" in severities:
            overall_severity = "HIGH"
        elif "MEDIUM" in severities:
            overall_severity = "MEDIUM"
        else:
            overall_severity = "LOW"

        incident_id = f"INC-CORR-{uuid.uuid4().hex[:8].upper()}"

        public_signals = []
        for s in pending:
            public_signals.append({
                "metric": s["metric"],
                "value": s["value"],
                "threshold": s["threshold"],
                "severity": s["severity"],
                "timestamp": s["timestamp"],
                "detection_method": s["detection_method"],
            })

        metrics_detected = sorted(list(set(s["metric"] for s in pending)))
        reason = f"Multiple correlated infrastructure signals detected ({', '.join(metrics_detected)})"

        correlated_incident = {
            "correlated": True,
            "incident_id": incident_id,
            "id": incident_id,
            "service": "aegis-api",
            "signals": public_signals,
            "signal_count": len(public_signals),
            "severity": overall_severity,
            "reason": reason,
            "timestamp": current_time.isoformat(),
            "incident_type": "CORRELATED",
            "status": "OPEN",
            "metric": "correlated_" + "_".join(metrics_detected),
            "value": max(s["value"] for s in public_signals),
            "threshold": 0.0,
            "correlation_window_seconds": self.window_seconds,
        }

        # Mark signals as consumed so they are not correlated repeatedly
        for s in pending:
            s["consumed"] = True

        self.correlated_incident_ids.append(incident_id)
        self.clear_expired(now=current_time)

        return correlated_incident

    def get_pending_signals_count(self, now: Optional[datetime] = None) -> int:
        """Return the count of active, unconsumed signals within the correlation window."""
        self.clear_expired(now=now)
        return len([s for s in self.signals if not s.get("consumed", False)])

    def get_signals(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Return current in-memory signals."""
        self.clear_expired(now=now)
        return [
            {k: v for k, v in s.items() if not k.startswith("_")}
            for s in self.signals
        ]

    def reset(self):
        """Reset the correlation engine state."""
        self.signals.clear()
        self.correlated_incident_ids.clear()
