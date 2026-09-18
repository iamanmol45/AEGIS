import os
from datetime import datetime


class RemediationPolicy:

    def __init__(self, max_desired_count=4, cooldown_seconds=120, critical_only=True):
        self.max_desired_count = max_desired_count
        self.cooldown_seconds = cooldown_seconds
        self.allowed_actions = [
            "SCALE_OUT",
            "RESTART_TASKS"
        ]
        self.critical_only = critical_only
        self.last_action_time = None

    def evaluate(self, incident, action, current_desired_count):
        # 1. Validate incident data
        if not incident or not isinstance(incident, dict):
            return {
                "allowed": False,
                "action": action,
                "reason": "Invalid or missing incident data"
            }


        incident_id = incident.get("id") if hasattr(incident, "get") else getattr(incident, "id", None)
        severity = incident.get("severity") if hasattr(incident, "get") else getattr(incident, "severity", None)

        if not incident_id or not severity:
            return {
                "allowed": False,
                "action": action,
                "reason": "Malformed incident data: missing id or severity"
            }

        # 2. Check Severity requirement (CRITICAL only for autonomous remediation)
        if self.critical_only and severity != "CRITICAL":
            return {
                "allowed": False,
                "action": action,
                "reason": f"Incident severity '{severity}' is not eligible for autonomous execution (CRITICAL required)"
            }

        # 3. Check Allowed Actions
        if action not in self.allowed_actions:
            return {
                "allowed": False,
                "action": action,
                "reason": f"Action '{action}' is not in allowed actions list {self.allowed_actions}"
            }

        # 4. Check Maximum Desired Task Count for SCALE_OUT
        if action == "SCALE_OUT":
            if current_desired_count >= self.max_desired_count:
                return {
                    "allowed": False,
                    "action": action,
                    "reason": f"Maximum desired task count reached ({self.max_desired_count})"
                }

        # 5. Check Cooldown Window for both SCALE_OUT and RESTART_TASKS
        if self.last_action_time is not None:
            elapsed = (datetime.utcnow() - self.last_action_time).total_seconds()
            if elapsed < self.cooldown_seconds:
                remaining = int(self.cooldown_seconds - elapsed)
                return {
                    "allowed": False,
                    "action": action,
                    "reason": f"Cooldown active: {remaining}s remaining"
                }

        return {
            "allowed": True,
            "action": action,
            "reason": "Policy checks passed"
        }

    def record_action(self):
        self.last_action_time = datetime.utcnow()

    def get_policy(self):
        return {
            "max_desired_count": self.max_desired_count,
            "cooldown_seconds": self.cooldown_seconds,
            "allowed_actions": self.allowed_actions,
            "critical_only": self.critical_only,
        }
