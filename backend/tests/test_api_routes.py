"""Route-shell tests with Rhea's functions faked (no Mongo). Owner: Enes."""
import pytest
from fastapi.testclient import TestClient

from api import factory_client
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


FAULTED = {"machine_id": "machine_b", "healthy": False, "fault_type": "SYNC_PROCESS_DOWN"}
HEALTHY = {"machine_id": "machine_b", "healthy": True, "fault_type": "NONE"}


def _factory(monkeypatch, health=None, error=None):
    async def get_health(machine_id):
        if error:
            raise factory_client.FactoryUnavailable(error)
        return health

    monkeypatch.setattr(factory_client, "get_health", get_health)


def test_create_run_returns_202(monkeypatch):
    _factory(monkeypatch, FAULTED)
    seen = {}

    async def enqueue_run(machine_id, crash_armed=False):
        seen["args"] = (machine_id, crash_armed)
        return "run_abc"

    monkeypatch.setattr(storage, "enqueue_run", enqueue_run)
    r = client.post("/api/runs", json={"machine_id": "machine_b", "crash_armed": True})
    assert r.status_code == 202
    assert r.json() == {"run_id": "run_abc", "status": "QUEUED"}
    assert seen["args"] == ("machine_b", True)


def test_create_run_409_when_machine_has_no_fault(monkeypatch):
    _factory(monkeypatch, HEALTHY)
    r = client.post("/api/runs", json={"machine_id": "machine_b"})
    assert r.status_code == 409
    assert "no fault" in r.json()["detail"]


def test_create_run_409_when_active_run_exists(monkeypatch):
    _factory(monkeypatch, FAULTED)

    class ActiveRunExistsError(Exception):
        pass

    async def enqueue_run(machine_id, crash_armed=False):
        raise ActiveRunExistsError("machine_b already has an active run")

    monkeypatch.setattr(storage, "ActiveRunExistsError", ActiveRunExistsError, raising=False)
    monkeypatch.setattr(storage, "enqueue_run", enqueue_run)
    r = client.post("/api/runs", json={"machine_id": "machine_b"})
    assert r.status_code == 409
    assert "active run" in r.json()["detail"]


def test_create_run_503_when_factory_unavailable(monkeypatch):
    _factory(monkeypatch, error="factory down")
    assert client.post("/api/runs", json={"machine_id": "machine_b"}).status_code == 503


def test_run_view_exposes_recovery_count(monkeypatch):
    async def get_run(run_id):
        return {**RUN, "recovery_count": 2}

    monkeypatch.setattr(ledger, "get_run", get_run)
    body = client.get("/api/runs/run_x").json()
    assert body["recovery_count"] == 2 and body["restart_count"] == 0


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


def test_factory_scenario_routes(monkeypatch):
    machines = [
        {
            "machine_id": machine_id, "healthy": True, "fault_type": "NONE",
            "sync_running": True, "network_connected": True, "backlog": 0,
            "fault_epoch": 0, "last_operation_id": None,
        }
        for machine_id in ("machine_a", "machine_b", "machine_c")
    ]
    seen = []

    async def list_machines():
        return machines

    async def break_sync(machine_id):
        seen.append(machine_id)
        return {**machines[1], "healthy": False, "fault_type": "SYNC_PROCESS_DOWN", "sync_running": False}

    async def has_active_runs():
        return False

    async def reset():
        return {"ok": True, "machines": machines}

    monkeypatch.setattr(factory_client, "list_machines", list_machines)
    monkeypatch.setattr(factory_client, "break_sync", break_sync)
    monkeypatch.setattr(factory_client, "reset", reset)
    monkeypatch.setattr(ledger, "has_active_runs", has_active_runs)

    assert len(client.get("/api/machines").json()) == 3
    response = client.post("/api/scenarios/sync_failure", json={"machine_id": "machine_b"})
    assert response.status_code == 200
    assert response.json()["healthy"] is False
    assert seen == ["machine_b"]
    assert client.post("/api/scenarios/sync_failure", json={"machine_id": "unknown"}).status_code == 422
    assert client.post("/api/scenarios/reset", json={"confirm": False}).status_code == 422
    assert client.post("/api/scenarios/reset", json={"confirm": True}).json()["ok"] is True


def test_reset_rejects_active_run(monkeypatch):
    async def has_active_runs():
        return True

    monkeypatch.setattr(ledger, "has_active_runs", has_active_runs)
    response = client.post("/api/scenarios/reset", json={"confirm": True})
    assert response.status_code == 409
