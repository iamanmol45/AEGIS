import pytest
from detector import AegisDetector
from incident import IncidentManager
from policy import RemediationPolicy
from controller import AegisController


@pytest.fixture
def detector():
    return AegisDetector(min_samples=10, max_history=60, contamination=0.05, random_state=42)


def test_normal_metrics_do_not_trigger_anomaly(detector):
    """Ensure typical idle/normal ECS values (CPU 0-5%, Memory 5-10%) do not trigger anomalies."""
    # Seed historical observations representing healthy baseline
    detector.add_history("cpu", [1.2, 2.0, 1.8, 3.1, 2.5, 1.9, 2.2, 3.0, 2.7, 1.5])
    detector.add_history("memory", [6.0, 6.2, 6.5, 6.1, 6.4, 6.3, 6.2, 6.5, 6.4, 6.1])

    cpu_res = detector.check_metric("cpu", 2.3)
    assert cpu_res["anomaly"] is False
    assert cpu_res["detection_method"] == "NORMAL"
    assert cpu_res["metric"] == "cpu"
    assert cpu_res["value"] == 2.3
    assert cpu_res["threshold"] == 80.0

    mem_res = detector.check_metric("memory", 6.8)
    assert mem_res["anomaly"] is False
    assert mem_res["detection_method"] == "NORMAL"
    assert mem_res["metric"] == "memory"


def test_cpu_threshold_anomaly(detector):
    """Verify static threshold triggers CRITICAL and HIGH anomalies when CPU > 80%."""
    res_high = detector.check_metric("cpu", 85.0)
    assert res_high["anomaly"] is True
    assert res_high["detection_method"] == "STATIC_THRESHOLD"
    assert res_high["severity"] == "HIGH"
    assert res_high["threshold"] == 80.0

    res_critical = detector.check_metric("cpu", 96.0)
    assert res_critical["anomaly"] is True
    assert res_critical["detection_method"] == "STATIC_THRESHOLD"
    assert res_critical["severity"] == "CRITICAL"


def test_memory_threshold_anomaly(detector):
    """Verify static threshold triggers anomaly when Memory > 80%."""
    res = detector.check_metric("memory", 88.0)
    assert res["anomaly"] is True
    assert res["detection_method"] == "STATIC_THRESHOLD"
    assert res["severity"] == "HIGH"
    assert res["value"] == 88.0


def test_insufficient_ml_history(detector):
    """Verify that with fewer than min_samples (10), ML anomaly detection remains inactive."""
    detector.reset_history()
    # Add only 3 observations
    detector.add_history("cpu", [2.0, 2.5, 3.0])

    res = detector.check_metric("cpu", 3.2)
    assert res["anomaly"] is False
    assert res["detection_method"] == "INSUFFICIENT_HISTORY"
    assert res["ml_score"] is None


def test_ml_isolation_forest_anomaly(detector):
    """Verify that a sudden statistical spike (e.g. 65% when baseline is 2-4%) is flagged by Isolation Forest."""
    detector.reset_history()
    # Seed 20 historical readings of calm traffic (2.0 - 4.5%)
    baseline = [2.1, 2.5, 3.0, 2.2, 2.8, 3.5, 2.4, 3.1, 2.9, 3.3,
                2.6, 2.7, 3.2, 3.0, 2.5, 2.8, 3.4, 2.3, 2.9, 3.1]
    detector.add_history("cpu", baseline)

    # Spike to 65% (below static 80% threshold, but an extreme statistical outlier)
    res = detector.check_metric("cpu", 65.0)
    assert res["anomaly"] is True
    assert res["detection_method"] == "ISOLATION_FOREST"
    assert res["ml_score"] is not None
    assert res["severity"] in ["HIGH", "MEDIUM"]
    assert "Isolation Forest" in res.get("details", "")


def test_task_failure_detection(detector):
    """Verify task failure detection when running_tasks < 1."""
    res_failed = detector.check_metric("running_tasks", 0)
    assert res_failed["anomaly"] is True
    assert res_failed["detection_method"] == "STATIC_THRESHOLD"
    assert res_failed["severity"] == "HIGH"

    res_healthy = detector.check_metric("running_tasks", 1)
    assert res_healthy["anomaly"] is False
    assert res_healthy["detection_method"] == "NORMAL"


def test_detector_output_compatibility_with_controller(detector):
    """Ensure detector output contains all required keys consumed by AegisController and IncidentManager."""
    incident_manager = IncidentManager()

    # Test static threshold output
    det_thresh = detector.check_metric("cpu", 95.0)
    assert "anomaly" in det_thresh
    assert "metric" in det_thresh
    assert "value" in det_thresh
    assert "threshold" in det_thresh
    assert "severity" in det_thresh

    inc1 = incident_manager.create_incident(
        metric=det_thresh["metric"],
        value=det_thresh["value"],
        threshold=det_thresh["threshold"],
        severity=det_thresh["severity"],
    )
    assert inc1.id.startswith("INC-")
    assert inc1.severity == "CRITICAL"

    # Test ML anomaly output
    detector.reset_history()
    detector.add_history("memory", [5.0, 5.2, 5.1, 5.3, 5.0, 5.4, 5.2, 5.1, 5.3, 5.2, 5.0, 5.2])
    det_ml = detector.check_metric("memory", 70.0)

    inc2 = incident_manager.create_incident(
        metric=det_ml["metric"],
        value=det_ml["value"],
        threshold=det_ml["threshold"],
        severity=det_ml["severity"],
    )
    assert inc2.metric == "memory"
    assert inc2.severity == "HIGH"


def test_ml_anomaly_does_not_bypass_policy_safety_gate():
    """Confirm that an ML anomaly (severity HIGH) is blocked by RemediationPolicy critical-only gate."""
    policy = RemediationPolicy(critical_only=True)
    detector = AegisDetector(min_samples=10)
    detector.add_history("cpu", [2.0] * 15)

    det = detector.check_metric("cpu", 65.0)
    incident = {
        "id": "INC-ML-TEST-001",
        "service": "aegis-api",
        "metric": det["metric"],
        "value": det["value"],
        "severity": det["severity"],  # "HIGH"
    }

    decision = policy.evaluate(incident, action="SCALE_OUT", current_desired_count=1)
    # Must be BLOCKED because only CRITICAL severity is permitted for autonomous scale-out
    assert decision["allowed"] is False
    assert "CRITICAL required" in decision["reason"]
