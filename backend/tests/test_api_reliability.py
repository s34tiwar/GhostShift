"""API routes end to end over the reliability layer. Owner: Rhea.

Real FastAPI app + real Mongo (this session's throwaway ghostshift_test_<hex> DB),
with the factory HTTP client replaced by an in-memory fake. All DB work runs on
TestClient's own event loop (client.portal), the same loop the API's Mongo client
lives on.
"""
import pytest
from fastapi.testclient import TestClient

from api import factory_client
from reliability import ledger, policies, storage
from tests.fakes import TEST_COLLECTIONS


class FakeFactoryApi:
    """Stands in for api.factory_client (health, break, reset)."""

    def __init__(self):
        self.fault = {"machine_a": "NONE", "machine_b": "SYNC_PROCESS_DOWN", "machine_c": "NONE"}
        self.resets = 0

    def _health(self, machine_id):
        fault = self.fault[machine_id]
        return {
            "machine_id": machine_id, "healthy": fault == "NONE", "fault_type": fault,
            "sync_running": fault != "SYNC_PROCESS_DOWN",
            "network_connected": fault != "NETWORK_DISCONNECTED",
            "backlog": 0 if fault == "NONE" else 125, "fault_epoch": 1,
            "last_operation_id": None, "restart_count": 0, "reconnect_count": 0,
            "message": "fake",
        }

    async def get_health(self, machine_id):
        return self._health(machine_id)

    async def break_sync(self, machine_id):
        self.fault[machine_id] = "SYNC_PROCESS_DOWN"
        return self._health(machine_id)

    async def reset(self):
        self.resets += 1
        self.fault = {m: "NONE" for m in self.fault}
        return {"ok": True, "machines": [self._health(m) for m in self.fault]}


@pytest.fixture
def api(test_db_name, monkeypatch):
    monkeypatch.setenv("MONGODB_DB", test_db_name)
    fake = FakeFactoryApi()
    for name in ("get_health", "break_sync", "reset"):
        monkeypatch.setattr(factory_client, name, getattr(fake, name))
    storage._client = None  # the API builds its own client on TestClient's loop

    from api.main import app

    with TestClient(app) as client:
        call = client.portal.call
        call(storage.init_indexes)
        call(policies.seed_default_policy)
        try:
            yield client, fake, call
        finally:
            for collection in TEST_COLLECTIONS:
                call(storage.get_db().drop_collection, collection)
            call(storage.close_client)


def _raw_run(call, run_id):
    return call(storage.agent_runs().find_one, {"_id": run_id})


def test_scenario_is_stored_on_the_run(api):
    client, _, call = api
    r = client.post("/api/runs", json={"machine_id": "machine_b", "scenario": "unsafe_shutdown"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "QUEUED" and body["run_id"].startswith("run_")
    assert _raw_run(call, body["run_id"])["scenario"] == "unsafe_shutdown"


def test_unsafe_scenario_on_healthy_machine_is_accepted(api):
    client, fake, _ = api
    assert fake.fault["machine_a"] == "NONE"
    no_scenario = client.post("/api/runs", json={"machine_id": "machine_a"})
    assert no_scenario.status_code == 409 and "no fault" in no_scenario.json()["detail"]
    r = client.post("/api/runs", json={"machine_id": "machine_a", "scenario": "unsafe_shutdown"})
    assert r.status_code == 202, r.text


def test_unknown_scenario_is_422(api):
    client, _, _ = api
    r = client.post("/api/runs", json={"machine_id": "machine_b", "scenario": "drop_database"})
    assert r.status_code == 422


def test_active_run_conflict_names_the_blocking_run(api):
    client, _, _ = api
    first = client.post("/api/runs", json={"machine_id": "machine_b"}).json()["run_id"]
    r = client.post("/api/runs", json={"machine_id": "machine_b"})
    assert r.status_code == 409
    assert first in r.json()["detail"]


def test_reset_without_force_while_run_active_is_409(api):
    client, fake, call = api
    run_id = client.post("/api/runs", json={"machine_id": "machine_b"}).json()["run_id"]
    r = client.post("/api/scenarios/reset", json={"confirm": True})
    assert r.status_code == 409 and "force" in r.json()["detail"]
    assert fake.resets == 0
    assert _raw_run(call, run_id)["status"] == "QUEUED"


def test_force_reset_aborts_run_and_frees_the_machine(api):
    client, fake, call = api
    run_id = client.post("/api/runs", json={"machine_id": "machine_b"}).json()["run_id"]

    r = client.post("/api/scenarios/reset", json={"confirm": True, "force": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["aborted_run_ids"] == [run_id]
    assert fake.resets == 1
    run = _raw_run(call, run_id)
    assert run["status"] == "FAILED" and run["abort_reason"] == "demo reset"

    # second forced reset aborts nothing
    assert client.post("/api/scenarios/reset", json={"confirm": True, "force": True}).json()["aborted_run_ids"] == []
    # the machine accepts a new run once it has a fault again
    assert client.post("/api/scenarios/sync_failure", json={"machine_id": "machine_b"}).status_code == 200
    again = client.post("/api/runs", json={"machine_id": "machine_b"})
    assert again.status_code == 202 and again.json()["run_id"] != run_id


def test_reset_without_active_runs_returns_empty_aborted_list(api):
    client, fake, _ = api
    r = client.post("/api/scenarios/reset", json={"confirm": True})
    assert r.status_code == 200 and r.json()["aborted_run_ids"] == []
    assert fake.resets == 1


def test_audit_response_includes_decision_and_detail(api):
    client, _, call = api
    run_id = client.post("/api/runs", json={"machine_id": "machine_b"}).json()["run_id"]
    proposal = {"tool": "shutdown_factory", "machine_id": "machine_b",
                "procedure_id": "proc_unsafe_demo", "rationale": "demo"}
    call(policies.authorize, run_id, "act_demo", proposal)

    r = client.get(f"/api/runs/{run_id}/audit")
    assert r.status_code == 200, r.text
    (event,) = r.json()["events"]
    assert event["event_type"] == "POLICY_CHECK"
    assert event["decision"] == "DENIED"
    assert event["detail"]["tool"] == "shutdown_factory"
    assert event["policy_version"] == 1


def test_audit_shows_force_reset_event(api):
    client, _, _ = api
    run_id = client.post("/api/runs", json={"machine_id": "machine_b"}).json()["run_id"]
    client.post("/api/scenarios/reset", json={"confirm": True, "force": True})
    events = client.get(f"/api/runs/{run_id}/audit").json()["events"]
    aborts = [e for e in events if e["event_type"] == "RUN_ABORTED_BY_RESET"]
    assert len(aborts) == 1
    assert aborts[0]["reason"] == "demo reset"
    assert aborts[0]["detail"] == {"previous_status": "QUEUED"}
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "FAILED"
