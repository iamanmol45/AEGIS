import boto3
from datetime import datetime, timedelta
from collections import deque
import numpy as np
from sklearn.ensemble import IsolationForest


class AegisDetector:

    def __init__(self, min_samples=10, max_history=60, contamination=0.05, random_state=42):
        self.thresholds = {
            "cpu": 80.0,
            "memory": 80.0,
            "running_tasks": 1,
            "latency_ms": 1000.0,
            "http_5xx_rate": 5.0,
            "db_connection_errors": 5.0,
            # Deliberately absent from rca.py's METRIC_KNOWLEDGE -- lets
            # this metric register as a real anomaly here while RCA still
            # treats it as a novel/unrecognized pattern (low confidence),
            # for the LOW_CONFIDENCE_INCIDENT chaos scenario.
            "disk_io_saturation": 70.0,
        }
        self.min_samples = min_samples
        self.max_history = max_history
        self.contamination = contamination
        self.random_state = random_state

        self.history = {
            "cpu": deque(maxlen=max_history),
            "memory": deque(maxlen=max_history),
        }

        self.cloudwatch = boto3.client(
            "cloudwatch",
            region_name="ap-south-1",
        )

    def reset_history(self, metric_name=None):
        """Reset the rolling history buffer for one or all metrics."""
        if metric_name:
            if metric_name in self.history:
                self.history[metric_name].clear()
        else:
            for key in self.history:
                self.history[key].clear()

    def add_history(self, metric_name, values):
        """Seed rolling history for testing or historical warmup."""
        if metric_name in self.history:
            for v in values:
                self.history[metric_name].append(float(v))

    def check_metric(self, metric_name, value):
        threshold = self.thresholds.get(metric_name)

        if threshold is None:
            return {
                "anomaly": False,
                "reason": "Unknown metric",
            }

        now_iso = datetime.utcnow().isoformat()
        float_val = float(value)

        # ----------------------------------------------------
        # 1. Task Failure Detection (Running Tasks Metric)
        # ----------------------------------------------------
        if metric_name == "running_tasks":
            abnormal = float_val < threshold
            if abnormal:
                return {
                    "anomaly": True,
                    "metric": metric_name,
                    "value": float_val,
                    "threshold": threshold,
                    "detection_method": "STATIC_THRESHOLD",
                    "severity": self.get_severity(metric_name, float_val),
                    "ml_score": None,
                    "timestamp": now_iso,
                }
            return {
                "anomaly": False,
                "metric": metric_name,
                "value": float_val,
                "threshold": threshold,
                "detection_method": "NORMAL",
                "severity": "LOW",
                "ml_score": None,
                "timestamp": now_iso,
            }

        # ----------------------------------------------------
        # 2. Safety Fallback: Static Threshold Check (CPU / Memory)
        # ----------------------------------------------------
        if float_val > threshold:
            if metric_name in self.history:
                self.history[metric_name].append(float_val)

            return {
                "anomaly": True,
                "metric": metric_name,
                "value": float_val,
                "threshold": threshold,
                "detection_method": "STATIC_THRESHOLD",
                "ml_score": None,
                "severity": self.get_severity(metric_name, float_val),
                "timestamp": now_iso,
            }

        # ----------------------------------------------------
        # 3. Machine Learning Detection: Isolation Forest
        # ----------------------------------------------------
        ml_anomaly = False
        ml_score = None
        metric_history = self.history.get(metric_name, deque())

        if len(metric_history) >= self.min_samples:
            history_data = np.array(list(metric_history), dtype=float).reshape(-1, 1)
            current_arr = np.array([[float_val]], dtype=float)
            dataset = np.vstack([history_data, current_arr])

            hist_mean = float(np.mean(history_data))
            hist_std = float(np.std(history_data))

            clf = IsolationForest(
                contamination=self.contamination,
                random_state=self.random_state,
                n_estimators=50,
            )
            clf.fit(dataset)

            raw_score = float(clf.decision_function(current_arr)[0])
            pred = int(clf.predict(current_arr)[0])  # -1 = anomaly, 1 = normal
            ml_score = round(raw_score, 4)

            # Conservative gating for Isolation Forest:
            # - Model identifies data point as an outlier (pred == -1)
            # - Value represents an upward surge above normal variation
            # - Value is meaningfully above the operational baseline floor (>= 20.0%)
            if pred == -1 and float_val > (hist_mean + 1.5 * max(hist_std, 2.0)) and float_val >= 20.0:
                ml_anomaly = True

        # Append current observation to history
        if metric_name in self.history:
            self.history[metric_name].append(float_val)

        if ml_anomaly:
            return {
                "anomaly": True,
                "metric": metric_name,
                "value": float_val,
                "threshold": threshold,
                "detection_method": "ISOLATION_FOREST",
                "ml_score": ml_score,
                "severity": "HIGH",
                "timestamp": now_iso,
                "details": f"Isolation Forest detected anomalous deviation from historical baseline (score: {ml_score})",
            }

        detection_method = "NORMAL" if len(metric_history) >= self.min_samples else "INSUFFICIENT_HISTORY"
        return {
            "anomaly": False,
            "metric": metric_name,
            "value": float_val,
            "threshold": threshold,
            "detection_method": detection_method,
            "severity": "LOW",
            "ml_score": ml_score,
            "timestamp": now_iso,
        }

    def get_severity(self, metric_name, value):
        if metric_name == "running_tasks":
            return "HIGH"

        # cpu/memory keep their original percentage-scale absolute
        # thresholds (load-bearing for existing tests/behavior).
        if metric_name in ("cpu", "memory"):
            if value >= 95:
                return "CRITICAL"
            if value >= 80:
                return "HIGH"
            return "MEDIUM"

        # Other metrics vary in unit/scale (ms, percent, count), so use a
        # ratio against their configured threshold instead of an absolute
        # cutoff tuned for a 0-100 percentage.
        threshold = self.thresholds.get(metric_name)
        if threshold:
            ratio = value / threshold
            if ratio >= 3:
                return "CRITICAL"
            if ratio >= 1.5:
                return "HIGH"
            return "MEDIUM"

        return "MEDIUM"

    def recheck_cleared(self, metric_name, dimensions):
        """Re-measures a live CloudWatch metric after a remediation attempt
        and reports whether it has actually dropped back under its
        threshold -- a counterfactual-style check ("did intervening on this
        service actually fix the symptom?", per CIRCA-SH's do-operator
        framing) rather than declaring recovery purely because ECS task
        counts converged. Only cpu/memory have a real CloudWatch series
        backing them; other metrics return checked=False rather than
        faking a result against data that was never real telemetry."""
        cw_metric_name = {"cpu": "CPUUtilization", "memory": "MemoryUtilization"}.get(metric_name)
        if cw_metric_name is None:
            return {"checked": False, "cleared": None, "current_value": None}

        try:
            value = self.get_latest_metric(cw_metric_name, "AWS/ECS", dimensions)
        except Exception:
            # A CloudWatch hiccup here shouldn't crash a remediation
            # attempt that otherwise succeeded -- fall back to "unmeasurable"
            # the same way a missing datapoint already does, and let the
            # caller fall back to ECS-level verification alone.
            value = None
        if value is None:
            return {"checked": False, "cleared": None, "current_value": None}

        threshold = self.thresholds.get(metric_name)
        return {
            "checked": True,
            "cleared": value <= threshold,
            "current_value": value,
            "threshold": threshold,
        }

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