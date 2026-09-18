from fastapi import FastAPI
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

app = FastAPI(
    title="AEGIS Autonomous Cloud Infrastructure",
    version="1.0.0",
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
aegis_controller = AegisController(policy=policy, workflow=workflow_manager)


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
                    "Value": "aegis-cluster",
                },
                {
                    "Name": "ServiceName",
                    "Value": "AegisInfrastructureStack-AegisApiServiceCEE6438E-NeHqjB9uxdtT",
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
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "metrics": results,
        "incidents": incidents,
    }


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

    if target is None:
        return {
            "verified": False,
            "reason": "No target desired count provided."
        }

    result = recovery_verifier.verify(target)

    return {
        "verified": result["recovered"],
        "recovery": result
    }


@app.post("/autonomous-run")
def autonomous_run():
    return aegis_controller.run()


@app.post("/workflow-test")
def workflow_test(incident: dict = None):
    if not incident:
        incident = {
            "id": "INC-WORKFLOW-TEST",
            "service": "aegis-api",
            "metric": "cpu",
            "value": 95,
            "threshold": 80,
            "severity": "CRITICAL",
        }

    # Fetch current ECS desired count
    ecs = remediation_engine.ecs
    service = ecs.describe_services(
        cluster=remediation_engine.cluster,
        services=[remediation_engine.service]
    )["services"][0]
    current_desired = service["desiredCount"]
    target_desired = current_desired + 1

    # 1. Evaluate policy
    policy_decision = policy.evaluate(
        incident=incident,
        action="SCALE_OUT",
        current_desired_count=current_desired
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

    # 2. Start Step Functions Execution
    exec_info = workflow_manager.start_recovery(
        incident=incident,
        action="SCALE_OUT",
        target_desired_count=target_desired
    )

    policy.record_action()

    # 3. Wait/Poll for execution completion
    workflow_result = workflow_manager.wait_for_completion(
        exec_info["execution_arn"],
        timeout_seconds=120
    )

    # 4. Audit
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

    # Fetch current ECS desired count
    ecs = remediation_engine.ecs
    service = ecs.describe_services(
        cluster=remediation_engine.cluster,
        services=[remediation_engine.service]
    )["services"][0]
    desired = service["desiredCount"]
    target_desired = desired if desired > 0 else 1

    # 1. Evaluate policy
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

    # 2. Start Step Functions Execution for RESTART_TASKS
    exec_info = workflow_manager.start_recovery(
        incident=incident,
        action="RESTART_TASKS",
        target_desired_count=target_desired
    )

    policy.record_action()

    # 3. Wait/Poll for execution completion
    workflow_result = workflow_manager.wait_for_completion(
        exec_info["execution_arn"],
        timeout_seconds=120
    )

    # 4. Audit
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

    import time

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

    import time

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
    ecs = remediation_engine.ecs

    service = ecs.describe_services(
        cluster=remediation_engine.cluster,
        services=[remediation_engine.service]
    )["services"][0]

    desired = service["desiredCount"]
    running = service["runningCount"]
    pending = service["pendingCount"]

    cpu = detector.get_latest_metric(
        "CPUUtilization",
        "AWS/ECS",
        [
            {"Name": "ClusterName", "Value": "aegis-cluster"},
            {
                "Name": "ServiceName",
                "Value": "AegisInfrastructureStack-AegisApiServiceCEE6438E-NeHqjB9uxdtT"
            },
        ],
    )

    memory = detector.get_latest_metric(
        "MemoryUtilization",
        "AWS/ECS",
        [
            {"Name": "ClusterName", "Value": "aegis-cluster"},
            {
                "Name": "ServiceName",
                "Value": "AegisInfrastructureStack-AegisApiServiceCEE6438E-NeHqjB9uxdtT"
            },
        ],
    )

    all_incidents = incident_manager.get_incidents()
    open_incidents = [
        i for i in all_incidents
        if i.get("status") == "OPEN"
    ]

    audit_logs = audit_logger.get_logs()
    last_event = audit_logs[-1] if audit_logs else None

    return {
        "system": "AEGIS Autonomous Cloud Infrastructure",
        "status": "OPERATIONAL",
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),

        "ecs_service": {
            "cluster": remediation_engine.cluster,
            "service": remediation_engine.service,
            "desired_count": desired,
            "running_count": running,
            "pending_count": pending,
            "healthy": running == desired and pending == 0,
        },

        "metrics": {
            "cpu_utilization": round(cpu, 2) if cpu is not None else None,
            "memory_utilization": round(memory, 2) if memory is not None else None,
        },

        "incidents": {
            "total": len(all_incidents),
            "open": len(open_incidents),
        },

        "remediation_guardrails": policy.get_policy(),
        "workflow": workflow_manager.get_status(),

        "last_audit_event": last_event,
    }