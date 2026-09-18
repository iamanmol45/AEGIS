import pytest
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from incident_store import IncidentStore
from incident import IncidentManager, Incident


@pytest.fixture
def store():
    # Uses in-memory fallback mirror when DynamoDB is offline/mocked
    return IncidentStore(table_name="AegisIncidentsTest")


def test_correlated_incident_persisted(store):
    """8. Correlated incident is persisted."""
    correlated_data = {
        "incident_id": "INC-CORR-TEST-001",
        "service": "aegis-api",
        "incident_type": "CORRELATED",
        "severity": "CRITICAL",
        "status": "OPEN",
        "signal_count": 2,
        "signals": [
            {"metric": "cpu", "value": 95.5, "threshold": 80.0, "severity": "CRITICAL"},
            {"metric": "memory", "value": 89.2, "threshold": 80.0, "severity": "CRITICAL"},
        ],
        "reason": "Multiple correlated infrastructure signals detected (cpu, memory)",
        "timestamp": datetime.utcnow().isoformat(),
    }

    created = store.create_incident(correlated_data)
    assert created["incident_id"] == "INC-CORR-TEST-001"

    # Retrieve from store
    retrieved = store.get_incident("INC-CORR-TEST-001")
    assert retrieved is not None
    assert retrieved["incident_id"] == "INC-CORR-TEST-001"
    assert retrieved["incident_type"] == "CORRELATED"
    assert retrieved["signal_count"] == 2
    assert len(retrieved["signals"]) == 2


def test_incident_retrieval_and_filtering(store):
    """9. Incident can be retrieved and filtered."""
    store.create_incident({
        "incident_id": "INC-001",
        "status": "OPEN",
        "severity": "CRITICAL",
        "metric": "cpu",
        "value": 96.0,
        "threshold": 80.0,
    })
    store.create_incident({
        "incident_id": "INC-002",
        "status": "RESOLVED",
        "severity": "HIGH",
        "metric": "memory",
        "value": 85.0,
        "threshold": 80.0,
    })

    open_incidents = store.get_incidents(status="OPEN")
    assert any(i["incident_id"] == "INC-001" for i in open_incidents)
    assert not any(i["incident_id"] == "INC-002" for i in open_incidents)


def test_existing_incident_manager_compatibility(store):
    """10. Existing IncidentManager behavior remains compatible."""
    manager = IncidentManager(store=store)

    inc = manager.create_incident(
        metric="cpu",
        value=95.0,
        threshold=80.0,
        severity="CRITICAL",
    )

    assert inc.id.startswith("INC-")
    assert inc["severity"] == "CRITICAL"
    assert inc.metric == "cpu"

    # Fetch by ID
    fetched = manager.get_by_id(inc.id)
    assert fetched is not None
    assert fetched.id == inc.id

    # Test Correlated Incident Creation through manager
    corr_inc = manager.create_correlated_incident({
        "incident_id": "INC-CORR-123",
        "signals": [{"metric": "cpu", "value": 95.0}, {"metric": "memory", "value": 90.0}],
        "signal_count": 2,
        "severity": "CRITICAL",
        "reason": "Test correlation",
    })
    assert corr_inc.id == "INC-CORR-123"
    assert corr_inc.incident_type == "CORRELATED"
    assert corr_inc.signal_count == 2
