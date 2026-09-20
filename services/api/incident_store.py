import os
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Any
from datetime import datetime
import boto3
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger("aegis.incident_store")


def _to_dynamodb_item(data: Any) -> Any:
    """Recursively convert float to Decimal for DynamoDB storage."""
    if isinstance(data, float):
        return Decimal(str(data))
    elif isinstance(data, dict):
        return {k: _to_dynamodb_item(v) for k, v in data.items() if v is not None}
    elif isinstance(data, list):
        return [_to_dynamodb_item(v) for v in data]
    return data


def _from_dynamodb_item(data: Any) -> Any:
    """Recursively convert Decimal back to float/int for Python application usage."""
    if isinstance(data, Decimal):
        if data % 1 == 0:
            return int(data)
        return float(data)
    elif isinstance(data, dict):
        return {k: _from_dynamodb_item(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_from_dynamodb_item(v) for v in data]
    return data


class IncidentStore:
    """
    Persistent DynamoDB Incident Store for AEGIS.
    Provides persistence for single and correlated incidents with graceful fallback.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
    ):
        self.table_name = table_name or os.getenv("DYNAMODB_TABLE_NAME", "AegisIncidents")
        self.region_name = region_name or os.getenv("AWS_REGION", "ap-south-1")
        self._fallback_memory: Dict[str, Dict[str, Any]] = {}
        self._dynamodb_available = True
        self.table = None

        try:
            dynamodb = boto3.resource("dynamodb", region_name=self.region_name)
            self.table = dynamodb.Table(self.table_name)
        except Exception as e:
            logger.warning(f"Could not initialize DynamoDB resource ({e}). Operating in memory-fallback mode.")
            self._dynamodb_available = False

    def create_incident(self, incident_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Store an incident in DynamoDB (or memory fallback).
        """
        if not isinstance(incident_data, dict):
            raise ValueError("incident_data must be a dictionary")

        incident_id = incident_data.get("incident_id") or incident_data.get("id")
        if not incident_id:
            raise ValueError("Incident must have an 'id' or 'incident_id'")

        record = dict(incident_data)
        record["incident_id"] = incident_id
        if "id" not in record:
            record["id"] = incident_id
        if "timestamp" not in record:
            record["timestamp"] = datetime.utcnow().isoformat()
        if "status" not in record:
            record["status"] = "OPEN"

        # Update in-memory fallback mirror
        self._fallback_memory[incident_id] = record

        if self.table and self._dynamodb_available:
            try:
                item = _to_dynamodb_item(record)
                self.table.put_item(Item=item)
            except (ClientError, BotoCoreError, Exception) as e:
                logger.warning(f"Failed to persist incident {incident_id} to DynamoDB: {e}. Saved in memory mirror.")

        return record

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a single incident by incident_id from DynamoDB or memory.
        """
        if self.table and self._dynamodb_available:
            try:
                response = self.table.get_item(Key={"incident_id": incident_id})
                item = response.get("Item")
                if item:
                    return _from_dynamodb_item(item)
            except (ClientError, BotoCoreError, Exception) as e:
                logger.warning(f"Failed to get incident {incident_id} from DynamoDB: {e}")

        # Fallback check
        return self._fallback_memory.get(incident_id)

    def get_incidents(
        self,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Scan and retrieve all incidents, optionally filtered by status.
        """
        incidents: List[Dict[str, Any]] = []

        if self.table and self._dynamodb_available:
            try:
                scan_kwargs: Dict[str, Any] = {"Limit": limit}
                if status:
                    scan_kwargs["FilterExpression"] = "#st = :st"
                    scan_kwargs["ExpressionAttributeNames"] = {"#st": "status"}
                    scan_kwargs["ExpressionAttributeValues"] = {":st": status.upper()}

                response = self.table.scan(**scan_kwargs)
                items = response.get("Items", [])
                incidents = [_from_dynamodb_item(i) for i in items]
                
                # Keep in-memory mirror updated with fetched items
                for inc in incidents:
                    inc_id = inc.get("incident_id") or inc.get("id")
                    if inc_id:
                        self._fallback_memory[inc_id] = inc
                        
            except (ClientError, BotoCoreError, Exception) as e:
                logger.warning(f"Failed to scan DynamoDB incidents: {e}. Falling back to memory.")
                incidents = []

        if not incidents:
            # Return from memory mirror
            mem_items = list(self._fallback_memory.values())
            if status:
                mem_items = [i for i in mem_items if str(i.get("status", "")).upper() == status.upper()]
            incidents = mem_items

        # Sort newest first
        incidents.sort(
            key=lambda x: str(x.get("timestamp", "")),
            reverse=True
        )
        return incidents[:limit]

    def get_items_by_type(
        self,
        record_type: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Scan and retrieve items tagged with a given `record_type` (used for
        non-incident records, e.g. chaos test summaries, that share this
        table so they get the same cross-replica persistence as incidents).
        """
        items: List[Dict[str, Any]] = []

        if self.table and self._dynamodb_available:
            try:
                response = self.table.scan(
                    FilterExpression="record_type = :rt",
                    ExpressionAttributeValues={":rt": record_type},
                    Limit=limit,
                )
                items = [_from_dynamodb_item(i) for i in response.get("Items", [])]
            except (ClientError, BotoCoreError, Exception) as e:
                logger.warning(f"Failed to scan DynamoDB for record_type={record_type}: {e}")
                items = []

        if not items:
            items = [
                i for i in self._fallback_memory.values()
                if i.get("record_type") == record_type
            ]

        items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        return items[:limit]

    def update_incident(
        self,
        incident_id: str,
        updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Update fields of an existing incident.
        """
        existing = self.get_incident(incident_id)
        if not existing:
            return None

        # Update dictionary
        updated_record = {**existing, **updates}
        self._fallback_memory[incident_id] = updated_record

        if self.table and self._dynamodb_available:
            try:
                item = _to_dynamodb_item(updated_record)
                self.table.put_item(Item=item)
            except (ClientError, BotoCoreError, Exception) as e:
                logger.warning(f"Failed to update incident {incident_id} in DynamoDB: {e}")

        return updated_record
