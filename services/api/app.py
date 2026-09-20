import os
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from policy import RemediationPolicy
from remediation import RemediationEngine
from detector import AegisDetector
from incident import IncidentManager
from rca import RCAEngine
from ai_reasoning import AIReasoningEngine
from recovery import RecoveryVerifier
from audit import AuditLogger
from controller import AegisController
from workflow import StepFunctionsWorkflowManager
from correlation import IncidentCorrelationEngine
from chaos import ChaosTestManager
from evidence import EvidenceStore

app = FastAPI(
    title="AEGIS Autonomous Cloud Infrastructure",
    version="1.0.0",
)

# The ALB is already open to the public internet with no auth on any route
# (see ARCHITECTURE_V2.md item 5), so permissive CORS doesn't widen the
# existing blast radius -- it just lets the browser dashboard read responses
# it could already fetch via curl.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

policy = RemediationPolicy()
workflow_manager = StepFunctionsWorkflowManager()
recovery_verifier = RecoveryVerifier()
remediation_engine = RemediationEngine(policy=policy)
detector = AegisDetector()
incident_manager = IncidentManager()
rca_engine = RCAEngine()
ai_reasoning_engine = AIReasoningEngine()
audit_logger = AuditLogger()
correlation_engine = IncidentCorrelationEngine(window_seconds=60)
evidence_store = EvidenceStore()
aegis_controller = AegisController(
    policy=policy,
    workflow=workflow_manager,
    correlation_engine=correlation_engine,
    incident_manager=incident_manager,
)
chaos_manager = ChaosTestManager(
    policy=policy,
    workflow=workflow_manager,
    correlation_engine=correlation_engine,
    incident_manager=incident_manager,
    detector=detector,
    remediation=remediation_engine,
    recovery=recovery_verifier,
    audit=audit_logger,
)


class MetricInput(BaseModel):
    metric: str
    value: float


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "aegis-detection",
    }


@app.get("/policy")
def get_policy():
    return policy.get_policy()


@app.get("/workflow")
def get_workflow():
    return workflow_manager.get_status()


@app.get("/correlation")
def get_correlation():
    """Expose multi-signal correlation configuration and pending signal status."""
    return {
        "correlation_window_seconds": correlation_engine.window_seconds,
        "pending_signals": correlation_engine.get_pending_signals_count(),
    }


@app.post("/correlation-test")
def correlation_test():
    """
    Simulate multiple anomalous signals (CPU 95% + Memory 90%)
    and verify that IncidentCorrelationEngine groups them into ONE unified correlated incident.
    Does NOT execute destructive ECS actions.
    """
    test_engine = IncidentCorrelationEngine(window_seconds=60)
    now_iso = datetime.utcnow().isoformat()

    s1 = {
        "metric": "cpu",
        "value": 95.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "detection_method": "STATIC_THRESHOLD",
        "timestamp": now_iso,
    }
    s2 = {
        "metric": "memory",
        "value": 90.0,
        "threshold": 80.0,
        "severity": "CRITICAL",
        "detection_method": "STATIC_THRESHOLD",
        "timestamp": now_iso,
    }

    test_engine.add_signal(s1)
    test_engine.add_signal(s2)

    correlated = test_engine.correlate()

    if correlated:
        incident = incident_manager.create_correlated_incident(correlated)
        audit_logger.log({
            "event": "CORRELATED_INCIDENT",
            "incident_id": incident.id,
            "severity": incident.severity,
            "signal_count": incident.signal_count or len(correlated.get("signals", [])),
            "signals": [s["metric"] for s in correlated.get("signals", [])],
            "reason": incident.reason or correlated.get("reason", "Multiple correlated infrastructure signals detected"),
        })

    return correlated


# ----------------------------------------------------
# Controlled Fault Injection & Chaos Demo Suite Routes
# ----------------------------------------------------

@app.get("/chaos")
def get_chaos():
    """Return available chaos test scenarios."""
    return {
        "available_tests": chaos_manager.get_available_tests()
    }


@app.post("/chaos/cpu-spike")
def chaos_cpu_spike(dry_run: bool = Query(default=False)):
    """Simulate a controlled CPU spike failure."""
    return chaos_manager.run_cpu_spike(dry_run=dry_run)


@app.post("/chaos/task-failure")
def chaos_task_failure(dry_run: bool = Query(default=False)):
    """Simulate a controlled ECS task crash/failure."""
    return chaos_manager.run_task_failure(dry_run=dry_run)


@app.post("/chaos/memory-pressure")
def chaos_memory_pressure(dry_run: bool = Query(default=False)):
    """Simulate a controlled container memory pressure anomaly."""
    return chaos_manager.run_memory_pressure(dry_run=dry_run)


@app.post("/chaos/multi-signal")
def chaos_multi_signal(dry_run: bool = Query(default=False)):
    """Simulate a controlled compound multi-signal failure."""
    return chaos_manager.run_multi_signal(dry_run=dry_run)


@app.post("/chaos/latency-spike")
def chaos_latency_spike(dry_run: bool = Query(default=False)):
    """Simulate a controlled API latency spike."""
    return chaos_manager.run_latency_spike(dry_run=dry_run)


@app.post("/chaos/error-rate-spike")
def chaos_error_rate_spike(dry_run: bool = Query(default=False)):
    """Simulate a controlled HTTP 5xx error rate spike."""
    return chaos_manager.run_error_rate_spike(dry_run=dry_run)


@app.post("/chaos/db-connectivity-failure")
def chaos_db_connectivity_failure(dry_run: bool = Query(default=False)):
    """Simulate a controlled RDS connectivity failure (expected to escalate, not auto-remediate)."""
    return chaos_manager.run_db_connectivity_failure(dry_run=dry_run)


@app.post("/chaos/bad-deployment")
def chaos_bad_deployment(dry_run: bool = Query(default=False)):
    """Simulate a controlled bad-deployment error spike."""
    return chaos_manager.run_bad_deployment(dry_run=dry_run)


@app.post("/chaos/low-confidence")
def chaos_low_confidence(dry_run: bool = Query(default=False)):
    """Simulate a detected anomaly with no established RCA pattern (expected to escalate on low confidence)."""
    return chaos_manager.run_low_confidence_incident(dry_run=dry_run)


@app.post("/chaos/normal-workload")
def chaos_normal_workload(dry_run: bool = Query(default=False)):
    """Simulate healthy metrics -- confirms no false-positive incident is raised."""
    return chaos_manager.run_normal_workload(dry_run=dry_run)


@app.get("/chaos/history")
def get_chaos_history():
    """Return recent chaos experiments."""
    return {
        "tests": chaos_manager.get_history()
    }


# ----------------------------------------------------
# Core Detection, Incidents & Operations Routes
# ----------------------------------------------------

@app.post("/detect")
def detect(metric: MetricInput):
    return detector.check_metric(
        metric.metric,
        metric.value,
    )


@app.get("/scan")
def scan():
    results = {}
    incidents = []

    metrics_to_check = [
        ("cpu", "CPUUtilization"),
        ("memory", "MemoryUtilization"),
    ]

    for metric_name, cloudwatch_metric in metrics_to_check:
        value = detector.get_latest_metric(
            metric_name=cloudwatch_metric,
            namespace="AWS/ECS",
            dimensions=[
                {
                    "Name": "ClusterName",
                    "Value": remediation_engine.cluster,
                },
                {
                    "Name": "ServiceName",
                    "Value": remediation_engine.service,
                },
            ],
        )

        if value is None:
            continue

        detection = detector.check_metric(
            metric_name,
            value,
        )

        results[metric_name] = detection

        if detection.get("anomaly"):
            incident = incident_manager.create_incident(
                metric=detection["metric"],
                value=detection["value"],
                threshold=detection["threshold"],
                severity=detection["severity"],
            )
            incidents.append(incident)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "metrics": results,
        "incidents": incidents,
    }


@app.post("/cycle")
def run_cycle():
    """
    Runs one full detect -> correlate -> policy -> recover -> verify cycle
    synchronously (same call supervisor.py's poll loop makes). Intended as
    the target for the EventBridge -> SQS -> Lambda alarm-triggered path,
    so a real anomaly gets a near-immediate cycle instead of waiting for
    the next fixed poll interval; the poller keeps running independently
    as a fallback in case an alarm-driven trigger is missed.
    """
    return aegis_controller.run()


@app.post("/incident")
def create_incident(metric: MetricInput):
    result = detector.check_metric(
        metric.metric,
        metric.value,
    )

    if not result.get("anomaly"):
        return {
            "incident_created": False,
            "reason": "No anomaly detected",
            "detection": result,
        }

    incident = incident_manager.create_incident(
        metric=result["metric"],
        value=result["value"],
        threshold=result["threshold"],
        severity=result["severity"],
    )

    return {
        "incident_created": True,
        "incident": incident,
    }


@app.get("/incidents")
def get_incidents():
    return {
        "incidents": incident_manager.get_incidents()
    }


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    incident = incident_manager.get_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.get("/incidents/{incident_id}/evidence")
def get_incident_evidence(incident_id: str):
    incident = incident_manager.get_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    evidence_key = (incident.metadata or {}).get("evidence_s3_key")
    if not evidence_key:
        raise HTTPException(status_code=404, detail="No archived evidence for this incident")

    evidence = evidence_store.get_evidence(evidence_key)
    if evidence is None:
        raise HTTPException(status_code=502, detail="Evidence archived but could not be retrieved from S3")

    return {"incident_id": incident_id, "evidence_s3_key": evidence_key, "evidence": evidence}


@app.post("/rca")
def analyze_incident(incident: dict):
    return rca_engine.analyze(incident)


@app.post("/reason")
def reason_incident(incident: dict):
    rca = rca_engine.analyze(incident)
    return ai_reasoning_engine.analyze(incident, rca)


@app.post("/remediate")
def remediate(incident: dict):
    execute_action = (
        incident.get("severity") == "CRITICAL"
        and incident.get("metric") in ["cpu", "memory"]
    )

    result = remediation_engine.scale_out(
        incident,
        execute=execute_action,
    )

    audit_logger.log({
        "event": "REMEDIATION",
        "incident_id": incident.get("id"),
        "service": incident.get("service"),
        "severity": incident.get("severity"),
        "metric": incident.get("metric"),
        "value": incident.get("value"),
        "action": result.get("action"),
        "policy_allowed": result.get("policy", {}).get("allowed", False),
        "policy_reason": result.get("policy", {}).get("reason", ""),
        "remediation_status": result.get("status"),
        "target_desired_count": result.get("target_desired_count"),
    })

    return {
        "action_taken": result.get("status") == "EXECUTED",
        "remediation": result,
    }


@app.get("/audit")
def get_audit():
    return {
        "events": audit_logger.get_logs()
    }


@app.post("/verify-recovery")
def verify_recovery(remediation: dict):
    target = remediation.get("target_desired_count") or remediation.get("desired_count")
    return recovery_verifier.verify(target)


@app.post("/step-function-test")
def step_function_test(incident: dict = None):
    if not incident:
        incident = {
            "id": "INC-WORKFLOW-TEST",
            "service": "aegis-api",
            "metric": "cpu",
            "value": 95,
            "threshold": 80,
            "severity": "CRITICAL",
        }

    ecs = remediation_engine.ecs
    services = ecs.describe_services(
        cluster=remediation_engine.cluster,
        services=[remediation_engine.service]
    ).get("services", [])
    desired = services[0]["desiredCount"] if services else 1
    target_desired = desired + 1

    policy_decision = policy.evaluate(
        incident=incident,
        action="SCALE_OUT",
        current_desired_count=desired
    )

    if not policy_decision["allowed"]:
        audit_logger.log({
            "event": "STEP_FUNCTION_RECOVERY",
            "incident_id": incident.get("id"),
            "action": "SCALE_OUT",
            "severity": incident.get("severity"),
            "metric": incident.get("metric"),
            "value": incident.get("value"),
            "policy_allowed": False,
            "policy_reason": policy_decision["reason"],
            "execution_arn": None,
            "workflow_status": "BLOCKED_BY_POLICY",
            "recovery_verified": False,
        })

        return {
            "incident": incident,
            "policy": policy_decision,
            "status": "BLOCKED_BY_POLICY",
            "execution": None,
            "workflow": None,
        }

    exec_info = workflow_manager.start_recovery(
        incident=incident,
        action="SCALE_OUT",
        target_desired_count=target_desired
    )

    policy.record_action()

    workflow_result = workflow_manager.wait_for_completion(
        exec_info["execution_arn"],
        timeout_seconds=150
    )

    audit_logger.log({
        "event": "STEP_FUNCTION_RECOVERY",
        "incident_id": incident.get("id"),
        "action": "SCALE_OUT",
        "severity": incident.get("severity"),
        "metric": incident.get("metric"),
        "value": incident.get("value"),
        "policy_allowed": True,
        "policy_reason": policy_decision["reason"],
        "execution_arn": exec_info["execution_arn"],
        "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
        "recovery_verified": workflow_result.get("recovery_verified", False),
    })

    return {
        "incident": incident,
        "policy": policy_decision,
        "execution": exec_info,
        "workflow": workflow_result,
    }


@app.post("/task-failure-workflow-test")
def task_failure_workflow_test(incident: dict = None):
    if not incident:
        incident = {
            "id": "INC-TASK-WORKFLOW-TEST",
            "service": "aegis-api",
            "metric": "task_failure",
            "value": 0,
            "threshold": 1,
            "severity": "CRITICAL",
        }

    ecs = remediation_engine.ecs
    services = ecs.describe_services(
        cluster=remediation_engine.cluster,
        services=[remediation_engine.service]
    ).get("services", [])
    desired = services[0]["desiredCount"] if services else 1
    target_desired = desired if desired > 0 else 1

    policy_decision = policy.evaluate(
        incident=incident,
        action="RESTART_TASKS",
        current_desired_count=desired
    )

    if not policy_decision["allowed"]:
        audit_logger.log({
            "event": "STEP_FUNCTION_RECOVERY",
            "incident_id": incident.get("id"),
            "action": "RESTART_TASKS",
            "severity": incident.get("severity"),
            "metric": incident.get("metric"),
            "value": incident.get("value"),
            "policy_allowed": False,
            "policy_reason": policy_decision["reason"],
            "execution_arn": None,
            "workflow_status": "BLOCKED_BY_POLICY",
            "recovery_verified": False,
        })

        return {
            "incident": incident,
            "policy": policy_decision,
            "status": "BLOCKED_BY_POLICY",
            "execution": None,
            "workflow": None,
        }

    exec_info = workflow_manager.start_recovery(
        incident=incident,
        action="RESTART_TASKS",
        target_desired_count=target_desired
    )

    policy.record_action()

    workflow_result = workflow_manager.wait_for_completion(
        exec_info["execution_arn"],
        timeout_seconds=150
    )

    audit_logger.log({
        "event": "STEP_FUNCTION_RECOVERY",
        "incident_id": incident.get("id"),
        "action": "RESTART_TASKS",
        "severity": incident.get("severity"),
        "metric": incident.get("metric"),
        "value": incident.get("value"),
        "policy_allowed": True,
        "policy_reason": policy_decision["reason"],
        "execution_arn": exec_info["execution_arn"],
        "workflow_status": workflow_result.get("workflow_status", "EXECUTED"),
        "recovery_verified": workflow_result.get("recovery_verified", False),
    })

    return {
        "incident": incident,
        "policy": policy_decision,
        "execution": exec_info,
        "workflow": workflow_result,
    }


@app.post("/autonomous-test")
def autonomous_test(incident: dict = None):
    if not incident:
        incident = {
            "id": "INC-AUTO-TEST",
            "service": "aegis-api",
            "metric": "cpu",
            "value": 95,
            "threshold": 80,
            "severity": "CRITICAL",
        }

    remediation = remediation_engine.scale_out(
        incident,
        execute=True,
    )

    target = remediation.get("target_desired_count")
    recovery = None

    if remediation.get("status") == "EXECUTED":
        for _ in range(12):
            recovery = recovery_verifier.verify(target)
            if recovery["recovered"]:
                break
            time.sleep(10)
    else:
        recovery = recovery_verifier.verify(
            remediation.get("previous_desired_count", 1)
        )

    audit_logger.log({
        "event": "AUTONOMOUS_RECOVERY",
        "incident_id": incident.get("id"),
        "action": remediation.get("action"),
        "severity": incident.get("severity"),
        "metric": incident.get("metric"),
        "value": incident.get("value"),
        "policy_allowed": remediation.get("policy", {}).get("allowed", False),
        "policy_reason": remediation.get("policy", {}).get("reason", ""),
        "remediation_status": remediation.get("status"),
        "recovery_verified": recovery["recovered"] if recovery else False,
        "target_desired_count": target,
    })

    return {
        "incident": incident,
        "remediation": remediation,
        "recovery": recovery,
    }


@app.post("/task-failure-test")
def task_failure_test(incident: dict = None):
    if not incident:
        incident = {
            "id": "INC-TASK-TEST",
            "service": "aegis-api",
            "metric": "task_failure",
            "value": 0,
            "threshold": 1,
            "severity": "CRITICAL",
        }

    remediation = remediation_engine.restart_tasks(incident, execute=True)

    recovery = None
    desired = remediation.get("desired_count", 1)

    if remediation.get("status") == "EXECUTED":
        for _ in range(12):
            recovery = recovery_verifier.verify(desired)
            if recovery["recovered"]:
                break
            time.sleep(10)
    else:
        recovery = recovery_verifier.verify(desired)

    audit_logger.log({
        "event": "AUTONOMOUS_TASK_RECOVERY",
        "incident_id": incident.get("id"),
        "action": remediation.get("action"),
        "severity": incident.get("severity"),
        "policy_allowed": remediation.get("policy", {}).get("allowed", False),
        "policy_reason": remediation.get("policy", {}).get("reason", ""),
        "remediation_status": remediation.get("status"),
        "recovery_verified": recovery["recovered"] if recovery else False,
    })

    return {
        "incident": incident,
        "remediation": remediation,
        "recovery": recovery,
    }


@app.get("/status")
def get_system_status():
    service_info = {
        "cluster": remediation_engine.cluster,
        "service": remediation_engine.service,
        "desired_count": 0,
        "running_count": 0,
        "pending_count": 0,
        "healthy": False,
    }

    try:
        ecs = remediation_engine.ecs
        desc = ecs.describe_services(
            cluster=remediation_engine.cluster,
            services=[remediation_engine.service]
        )
        services = desc.get("services", [])
        if services:
            service = services[0]
            desired = service.get("desiredCount", 0)
            running = service.get("runningCount", 0)
            pending = service.get("pendingCount", 0)
            service_info.update({
                "desired_count": desired,
                "running_count": running,
                "pending_count": pending,
                "healthy": running == desired and pending == 0,
            })
    except Exception as e:
        service_info["error"] = str(e)

    cpu = None
    memory = None
    try:
        cpu = detector.get_latest_metric(
            "CPUUtilization",
            "AWS/ECS",
            [
                {"Name": "ClusterName", "Value": remediation_engine.cluster},
                {"Name": "ServiceName", "Value": remediation_engine.service},
            ],
        )
        memory = detector.get_latest_metric(
            "MemoryUtilization",
            "AWS/ECS",
            [
                {"Name": "ClusterName", "Value": remediation_engine.cluster},
                {"Name": "ServiceName", "Value": remediation_engine.service},
            ],
        )
    except Exception:
        pass

    all_incidents = incident_manager.get_incidents()
    open_incidents = [
        i for i in all_incidents
        if (i.status if hasattr(i, "status") else i.get("status")) == "OPEN"
    ]

    audit_logs = audit_logger.get_logs()
    last_event = audit_logs[-1] if audit_logs else None

    return {
        "system": "AEGIS Autonomous Cloud Infrastructure",
        "status": "OPERATIONAL",
        "timestamp": datetime.utcnow().isoformat(),
        "ecs_service": service_info,
        "metrics": {
            "cpu_utilization": round(cpu, 2) if cpu is not None else None,
            "memory_utilization": round(memory, 2) if memory is not None else None,
        },
        "incidents": {
            "total": len(all_incidents),
            "open": len(open_incidents),
        },
        "correlation": {
            "window_seconds": correlation_engine.window_seconds,
            "pending_signals": correlation_engine.get_pending_signals_count(),
        },
        "chaos_suite": {
            "available_tests": chaos_manager.get_available_tests(),
            "total_experiments_run": len(chaos_manager.get_history()),
        },
        "remediation_guardrails": policy.get_policy(),
        "workflow": workflow_manager.get_status(),
        "last_audit_event": last_event,
    }