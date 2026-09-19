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