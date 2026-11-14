"""API routes against a real MongoDB (private test DB). Owner: Enes.

Skipped when neither BACKEND_MONGODB_URI nor legacy MONGODB_URI is set.
Never touches the shared hackathon DB.
"""
import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()
pytestmark = pytest.mark.skipif(
    not (os.environ.get("BACKEND_MONGODB_URI") or os.environ.get("MONGODB_URI")),
    reason="BACKEND_MONGODB_URI/MONGODB_URI not set",
)

TEST_DB = "ghostshift_test_enes"
COLLECTIONS = ("agent_runs", "action_ledger", "audit_events", "policies")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MONGODB_DB", TEST_DB)
    from api import factory_client
    from api.main import app
    from reliability import storage

    async def clean():
        db = storage.get_db()
        assert db.name == TEST_DB
        for name in COLLECTIONS:
            await db.drop_collection(name)

    async def faulted(machine_id):
        return {"machine_id": machine_id, "healthy": False, "fault_type": "SYNC_PROCESS_DOWN"}

    monkeypatch.setattr(factory_client, "get_health", faulted)  # factory not deployed yet

    with TestClient(app) as c:
        c.portal.call(storage.close_client)  # client bound to this loop
        yield c
        c.portal.call(clean)
        c.portal.call(storage.close_client)


def _create(client, **body):
    r = client.post("/api/runs", json={"machine_id": "machine_b", **body})
    assert r.status_code == 202
    return r.json()["run_id"]


def test_create_then_read_run(client):
    run_id = _create(client)
    assert run_id.startswith("run_")
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "QUEUED"
    assert run["machine_id"] == "machine_b"
    assert run["restart_count"] == 0


def test_audit_and_actions_empty_for_new_run(client):
    run_id = _create(client)
    assert client.get(f"/api/runs/{run_id}/audit").json() == {"events": []}
    assert client.get(f"/api/runs/{run_id}/actions").json() == []


def test_crash_arm_sets_flag(client):
    from reliability import storage

    run_id = _create(client)
    body = {"run_id": run_id, "stage": "after_effect_before_verify"}
    assert client.post("/api/demo/crash-arm", json=body).json() == {"armed": True}
    doc = client.portal.call(storage.agent_runs().find_one, {"_id": run_id})
    assert doc["crash_armed"] is True


def test_resume_keeps_run_id_and_moves_to_reconciliation(client):
    run_id = _create(client)
    r = client.post(f"/api/runs/{run_id}/resume")
    assert r.json() == {"run_id": run_id, "status": "NEEDS_RECONCILIATION"}
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "NEEDS_RECONCILIATION"


def test_resume_is_noop_for_completed_run(client):
    from reliability import ledger

    run_id = _create(client)
    client.portal.call(ledger.set_run_status, run_id, "COMPLETED")
    r = client.post(f"/api/runs/{run_id}/resume")
    assert r.status_code == 200
    assert r.json()["status"] == "COMPLETED"


def test_unknown_run_is_404_everywhere(client):
    assert client.get("/api/runs/run_nope").status_code == 404
    assert client.get("/api/runs/run_nope/audit").status_code == 404
    assert client.get("/api/runs/run_nope/actions").status_code == 404
    assert client.post("/api/runs/run_nope/resume").status_code == 404
    body = {"run_id": "run_nope", "stage": "after_effect_before_verify"}
    assert client.post("/api/demo/crash-arm", json=body).status_code == 404
