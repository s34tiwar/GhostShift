"""Factory contract and idempotency tests. Owner: Arav."""

from __future__ import annotations

import threading

import pytest

HEALTH_FIELDS = {
    "machine_id", "healthy", "fault_type", "sync_running", "network_connected", "backlog",
    "fault_epoch", "last_operation_id", "restart_count", "reconnect_count", "message",
}
REPAIR_FIELDS = {
    "operation_id", "machine_id", "action_type", "applied", "replayed", "effect_present", "reason",
    "fault_epoch", "fault_type", "healthy", "restart_count", "reconnect_count",
}
VERIFY_FIELDS = {
    "effect_present", "receipt_exists", "receipt_applied", "receipt_epoch", "state_effect_present",
    "operation_id", "healthy", "fault_type", "fault_epoch", "restart_count", "reconnect_count",
    "expected_action",
}


def brk(c, m="machine_b", fault="SYNC_PROCESS_DOWN"):
    r = c.post(f"/machines/{m}/break", json={"fault_type": fault})
    assert r.status_code == 200, r.text
    return r.json()


def restart(c, op, m="machine_b"):
    return c.post(f"/machines/{m}/restart-sync", json={"operation_id": op})


def reconnect(c, op, m="machine_b"):
    return c.post(f"/machines/{m}/reconnect", json={"operation_id": op})


def verify(c, action="restart_sync", op=None, m="machine_b"):
    params = {"expected_action": action}
    if op:
        params["operation_id"] = op
    r = c.get(f"/machines/{m}/verify", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- basics ----------
def test_service_health(factory_client):
    assert factory_client.get("/health").json() == {"ok": True, "service": "factory"}


def test_machines_start_healthy_with_all_fields(factory_client):
    machines = factory_client.get("/machines").json()
    assert [m["machine_id"] for m in machines] == ["machine_a", "machine_b", "machine_c"]
    for m in machines:
        assert set(m) == HEALTH_FIELDS
        assert m["healthy"] is True and m["fault_type"] == "NONE" and m["backlog"] == 0


def test_unknown_machine_404(factory_client):
    assert factory_client.get("/machines/machine_z/health").status_code == 404
    assert restart(factory_client, "op1", m="machine_z").status_code == 404


def test_bad_input_422(factory_client):
    c = factory_client
    assert c.post("/machines/machine_b/break", json={"fault_type": "ON_FIRE"}).status_code == 422
    assert c.post("/machines/machine_b/restart-sync", json={}).status_code == 422
    assert c.post("/machines/machine_b/restart-sync", json={"operation_id": ""}).status_code == 422
    assert c.get("/machines/machine_b/verify", params={"expected_action": "shutdown_factory"}).status_code == 422


# ---------- break ----------
@pytest.mark.parametrize("fault,flag", [("SYNC_PROCESS_DOWN", "sync_running"), ("NETWORK_DISCONNECTED", "network_connected")])
def test_break_sets_fault_and_bumps_epoch(factory_client, fault, flag):
    h = brk(factory_client, fault=fault)
    assert h["healthy"] is False and h["fault_type"] == fault and h[flag] is False
    assert h["backlog"] == 125 and h["fault_epoch"] == 1
    assert brk(factory_client, fault=fault)["fault_epoch"] == 2


def test_fault_messages_are_stable_and_distinct(factory_client):
    a = brk(factory_client, m="machine_a", fault="SYNC_PROCESS_DOWN")["message"]
    c = brk(factory_client, m="machine_c", fault="NETWORK_DISCONNECTED")["message"]
    b = factory_client.get("/machines/machine_b/health").json()["message"]
    assert len({a, b, c}) == 3
    assert brk(factory_client, m="machine_a", fault="SYNC_PROCESS_DOWN")["message"] == a


# ---------- repair + idempotency ----------
def test_restart_sync_repairs_once(factory_client):
    brk(factory_client)
    r = restart(factory_client, "run_x:machine_b:restart_sync:epoch_1").json()
    assert set(r) == REPAIR_FIELDS
    assert r["applied"] is True and r["replayed"] is False and r["effect_present"] is True
    assert r["healthy"] is True and r["restart_count"] == 1 and r["fault_type"] == "NONE"
    h = factory_client.get("/machines/machine_b/health").json()
    assert h["backlog"] == 0 and h["last_operation_id"] == "run_x:machine_b:restart_sync:epoch_1"


def test_same_operation_id_replays_without_second_repair(factory_client):
    brk(factory_client)
    op = "run_x:machine_b:restart_sync:epoch_1"
    first = restart(factory_client, op).json()
    for _ in range(3):
        again = restart(factory_client, op).json()
        assert again["replayed"] is True and again["applied"] is True
        assert again["restart_count"] == 1
    assert first["replayed"] is False


def test_reconnect_repairs_network_fault(factory_client):
    brk(factory_client, fault="NETWORK_DISCONNECTED")
    r = reconnect(factory_client, "op_net").json()
    assert r["applied"] is True and r["reconnect_count"] == 1 and r["restart_count"] == 0 and r["healthy"] is True


@pytest.mark.parametrize("fault,wrong", [("NETWORK_DISCONNECTED", restart), ("SYNC_PROCESS_DOWN", reconnect)])
def test_wrong_repair_is_not_applied(factory_client, fault, wrong):
    brk(factory_client, fault=fault)
    r = wrong(factory_client, "op_wrong")
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False and body["reason"] == "wrong_action_for_fault"
    assert body["fault_type"] == fault and body["healthy"] is False
    assert body["restart_count"] == 0 and body["reconnect_count"] == 0


def test_repair_on_healthy_machine_is_noop(factory_client):
    r = restart(factory_client, "op_noop").json()
    assert r["applied"] is False and r["reason"] == "already_healthy" and r["restart_count"] == 0


def test_operation_id_reused_for_other_machine_or_action_409(factory_client):
    brk(factory_client)
    brk(factory_client, m="machine_c")
    assert restart(factory_client, "op_shared").status_code == 200
    assert restart(factory_client, "op_shared", m="machine_c").status_code == 409
    assert reconnect(factory_client, "op_shared").status_code == 409


def test_new_fault_epoch_needs_new_operation(factory_client):
    brk(factory_client)
    restart(factory_client, "op_e1")
    brk(factory_client)  # epoch 2
    replay = restart(factory_client, "op_e1").json()
    assert replay["replayed"] is True and replay["healthy"] is False  # old receipt doesn't fix new fault
    r = restart(factory_client, "op_e2").json()
    assert r["applied"] is True and r["restart_count"] == 2


def test_concurrent_duplicates_apply_exactly_once(factory_client):
    brk(factory_client)
    results, errors = [], []

    def go():
        try:
            results.append(restart(factory_client, "op_race").json())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert all(r["applied"] for r in results)
    assert factory_client.get("/machines/machine_b/health").json()["restart_count"] == 1


# ---------- verify ----------
def test_verify_before_and_after_repair(factory_client):
    brk(factory_client)
    v = verify(factory_client, op="op_v")
    assert set(v) - {"machine_id"} == VERIFY_FIELDS
    assert v["effect_present"] is False and v["receipt_exists"] is False and v["receipt_epoch"] is None
    restart(factory_client, "op_v")
    v = verify(factory_client, op="op_v")
    assert v["effect_present"] is True and v["receipt_applied"] is True and v["receipt_epoch"] == 1
    assert v["state_effect_present"] is True


def test_verify_without_operation_id_uses_last_operation(factory_client):
    brk(factory_client)
    restart(factory_client, "op_last")
    v = verify(factory_client)
    assert v["effect_present"] is True and v["operation_id"] == "op_last"


def test_verify_false_for_wrong_action_or_stale_epoch(factory_client):
    brk(factory_client)
    restart(factory_client, "op_s")
    assert verify(factory_client, action="reconnect", op="op_s")["effect_present"] is False
    brk(factory_client)  # new fault: old receipt is stale
    v = verify(factory_client, op="op_s")
    assert v["effect_present"] is False and v["receipt_exists"] is True and v["receipt_epoch"] == 1


def test_verify_booleans_never_null(factory_client):
    v = verify(factory_client)
    for k in ("effect_present", "receipt_exists", "receipt_applied", "state_effect_present", "healthy"):
        assert isinstance(v[k], bool)


# ---------- reset / persistence / auth ----------
def test_reset_clears_everything(factory_client):
    brk(factory_client)
    restart(factory_client, "op_r")
    brk(factory_client, m="machine_c", fault="NETWORK_DISCONNECTED")
    r = factory_client.post("/simulation/reset").json()
    assert r["ok"] is True
    for m in r["machines"]:
        assert m["healthy"] is True and m["restart_count"] == 0 and m["fault_epoch"] == 0
    assert verify(factory_client, op="op_r")["receipt_exists"] is False


def test_restart_does_not_reset_broken_machine(collection):
    from fastapi.testclient import TestClient

    from backend.factory.main import create_app
    from backend.factory.mongo_store import FactoryStore

    with TestClient(create_app(lambda: FactoryStore(collection))) as c:
        brk(c)
    with TestClient(create_app(lambda: FactoryStore(collection))) as c:  # "redeploy"
        h = c.get("/machines/machine_b/health").json()
        assert h["healthy"] is False and h["fault_epoch"] == 1


def test_token_required_on_posts_when_set(factory_client, monkeypatch):
    monkeypatch.setenv("FACTORY_TOKEN", "s3cret")
    body = {"fault_type": "SYNC_PROCESS_DOWN"}
    assert factory_client.post("/machines/machine_b/break", json=body).status_code == 401
    assert factory_client.post("/machines/machine_b/break", json=body, headers={"X-Factory-Token": "nope"}).status_code == 401
    assert factory_client.post("/machines/machine_b/break", json=body, headers={"X-Factory-Token": "s3cret"}).status_code == 200
    assert factory_client.get("/machines").status_code == 200  # GETs stay open
