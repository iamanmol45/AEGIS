import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
import boto3
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger("aegis.evidence")


class EvidenceStore:
    """
    Archives raw incident evidence (correlated signals, metric detections,
    RCA payloads) to S3 so DynamoDB only has to hold a reference, not the
    full blob -- the spec's "Evidence Storage" requirement.
    """

    def __init__(self, bucket_name: Optional[str] = None, region_name: Optional[str] = None):
        self.bucket_name = bucket_name or os.getenv("EVIDENCE_BUCKET_NAME")
        self.region_name = region_name or os.getenv("AWS_REGION", "ap-south-1")
        self._s3 = None
        if self.bucket_name:
            try:
                self._s3 = boto3.client("s3", region_name=self.region_name)
            except Exception as e:
                logger.warning(f"Could not initialize S3 client ({e}). Evidence archiving disabled.")

    @property
    def enabled(self) -> bool:
        return self._s3 is not None and bool(self.bucket_name)

    def put_evidence(self, incident_id: str, evidence: Dict[str, Any]) -> Optional[str]:
        """Uploads raw evidence for an incident. Returns the S3 key, or None
        if archiving is unavailable/fails -- never raises, since a missed
        evidence upload shouldn't block detection/recovery."""
        if not self.enabled:
            return None

        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        key = f"incidents/{incident_id}/{timestamp}.json"

        try:
            self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=json.dumps(evidence, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            return key
        except (ClientError, BotoCoreError, Exception) as e:
            logger.warning(f"Failed to archive evidence for {incident_id} to S3: {e}")
            return None

    def get_evidence(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            return json.loads(response["Body"].read())
        except (ClientError, BotoCoreError, Exception) as e:
            logger.warning(f"Failed to fetch evidence {key} from S3: {e}")
            return None
