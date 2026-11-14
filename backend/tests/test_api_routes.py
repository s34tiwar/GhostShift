"""Route-shell tests with Rhea's functions faked (no Mongo). Owner: Enes."""
import pytest
from fastapi.testclient import TestClient

from api.main import app
from reliability import ledger, storage

client = TestClient(app)

RUN = {
    "run_id": "run_x", "status": "RUNNING", "machine_id": "machine_b",
    "procedure_id": None, "retrieval_mode": None, "current_action_id": None,
    "restart_count": 0, "worker_status": "IDLE",
}


@pytest.fixture
def fake_run(monkeypatch):
    async def get_run(run_id):
        return RUN if run_id == "run_x" else None

    async def list_audit(run_id):
        return {"events": [{"at": "t", "actor": "agent", "event_type": "RUN_QUEUED"}]}

    async def get_machine_ledger(run_id):
        return []

    monkeypatch.setattr(ledger, "get_run", get_run)
    monkeypatch.setattr(ledger, "list_audit", list_audit)
    monkeypatch.setattr(ledger, "get_machine_ledger", get_machine_ledger)


def test_reads_call_ledger(fake_run):
    assert client.get("/api/runs/run_x").json()["status"] == "RUNNING"
    assert client.get("/api/runs/run_x/audit").json()["events"][0]["event_type"] == "RUN_QUEUED"
    assert client.get("/api/runs/run_x/actions").json() == []


def test_reads_404_for_unknown_run(fake_run):
    for suffix in ("", "/audit", "/actions"):
        assert client.get(f"/api/runs/nope{suffix}").status_code == 404


def test_create_run_returns_202(monkeypatch):
    seen = {}

    async def enqueue_run(machine_id, crash_armed=False):
        seen["args"] = (machine_id, crash_armed)
        return "run_abc"

    monkeypatch.setattr(storage, "enqueue_run", enqueue_run)
    r = client.post("/api/runs", json={"machine_id": "machine_b", "crash_armed": True})
    assert r.status_code == 202
    assert r.json() == {"run_id": "run_abc", "status": "QUEUED"}
    assert seen["args"] == ("machine_b", True)


def test_create_run_rejects_unknown_machine():
    assert client.post("/api/runs", json={"machine_id": "machine_z"}).status_code == 422


def test_resume_and_crash_arm(monkeypatch):
    async def resume_run(run_id):
        if run_id != "run_x":
            raise ValueError("run does not exist")
        return {"run_id": run_id, "status": "NEEDS_RECONCILIATION"}

    async def arm_crash(run_id, stage):
        if run_id != "run_x":
            raise ValueError("run does not exist")
        return {"armed": True}

    monkeypatch.setattr(ledger, "resume_run", resume_run)
    monkeypatch.setattr(ledger, "arm_crash", arm_crash)
    assert client.post("/api/runs/run_x/resume").json()["status"] == "NEEDS_RECONCILIATION"
    assert client.post("/api/runs/nope/resume").status_code == 404
    body = {"run_id": "run_x", "stage": "after_effect_before_verify"}
    assert client.post("/api/demo/crash-arm", json=body).json() == {"armed": True}
    assert client.post("/api/demo/crash-arm", json={**body, "run_id": "nope"}).status_code == 404
    assert client.post("/api/demo/crash-arm", json={**body, "stage": "bad"}).status_code == 422
