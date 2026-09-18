import json
import os
from datetime import datetime


class AuditLogger:

    def __init__(self):
        self.file = os.getenv("AUDIT_FILE", "audit.log")

    def log(self, event):
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            **event
        }

        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        return record

    def get_logs(self):
        if not os.path.exists(self.file):
            return []

        with open(self.file, "r", encoding="utf-8") as f:
            return [
                json.loads(line)
                for line in f
                if line.strip()
            ]