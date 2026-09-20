import boto3
import json
import os
import time
from datetime import datetime


class StepFunctionsWorkflowManager:

    def __init__(self, state_machine_name="aegis-recovery-workflow", region_name=None):
        self.region = region_name or os.getenv("AWS_REGION", "ap-south-1")
        self.sfn = boto3.client("stepfunctions", region_name=self.region)
        self.state_machine_name = state_machine_name
        self._state_machine_arn = os.getenv("STATE_MACHINE_ARN")

    @property
    def state_machine_arn(self):
        if self._state_machine_arn:
            return self._state_machine_arn

        # Dynamically discover state machine ARN if not set
        try:
            paginator = self.sfn.get_paginator("list_state_machines")
            for page in paginator.paginate():
                for sm in page.get("stateMachines", []):
                    if sm["name"] == self.state_machine_name:
                        self._state_machine_arn = sm["stateMachineArn"]
                        return self._state_machine_arn
        except Exception as e:
            print(f"Error discovering state machine ARN: {e}")

        # Fallback ARN pattern
        return f"arn:aws:states:{self.region}:*:stateMachine:{self.state_machine_name}"

    def start_recovery(self, incident, action, target_desired_count):
        arn = self.state_machine_arn
        incident_id = incident.get("id") if hasattr(incident, "get") else incident["id"]
        safe_incident_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(incident_id))
        exec_name = f"aegis-{safe_incident_id}-{int(time.time())}"

        payload = {
            "incident_id": str(incident_id),
            "service": incident.get("service", "aegis-api") if hasattr(incident, "get") else "aegis-api",
            "metric": incident.get("metric", "cpu") if hasattr(incident, "get") else "cpu",
            "value": incident.get("value", 0) if hasattr(incident, "get") else 0,
            "threshold": incident.get("threshold", 0) if hasattr(incident, "get") else 0,
            "severity": incident.get("severity", "CRITICAL") if hasattr(incident, "get") else "CRITICAL",
            "action": action,
            "target_desired_count": int(target_desired_count),
        }

        response = self.sfn.start_execution(
            stateMachineArn=arn,
            name=exec_name,
            input=json.dumps(payload),
        )

        return {
            "execution_arn": response["executionArn"],
            "start_date": response["startDate"].isoformat(),
            "incident_id": incident_id,
            "action": action,
            "target_desired_count": target_desired_count,
        }

    def wait_for_completion(self, execution_arn, timeout_seconds=150, poll_interval=5):
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            desc = self.sfn.describe_execution(executionArn=execution_arn)
            status = desc["status"]

            if status != "RUNNING":
                output = {}
                if "output" in desc:
                    try:
                        output = json.loads(desc["output"])
                    except Exception:
                        output = {"raw_output": desc["output"]}

                return {
                    "execution_arn": execution_arn,
                    "status": status,
                    "stop_date": desc.get("stopDate", datetime.utcnow()).isoformat() if hasattr(desc.get("stopDate"), "isoformat") else str(desc.get("stopDate")),
                    "output": output,
                    "workflow_status": output.get("workflow_status", status),
                    "recovery_verified": output.get("recovery_verified", status == "SUCCEEDED"),
                }

            time.sleep(poll_interval)

        return {
            "execution_arn": execution_arn,
            "status": "TIMED_OUT",
            "workflow_status": "TIMED_OUT",
            "recovery_verified": False,
            "reason": f"Execution timed out after {timeout_seconds} seconds.",
        }

    def get_status(self):
        arn = self.state_machine_arn
        sm_status = "ACTIVE"
        try:
            desc = self.sfn.describe_state_machine(stateMachineArn=arn)
            sm_status = desc.get("status", "ACTIVE")
        except Exception:
            pass

        return {
            "state_machine": self.state_machine_name,
            "state_machine_arn": arn,
            "status": sm_status,
            "supported_actions": [
                "SCALE_OUT",
                "RESTART_TASKS"
            ],
        }
