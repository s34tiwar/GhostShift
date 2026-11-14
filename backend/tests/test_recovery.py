"""Ledger, policy, crash, and reconciliation tests. Owner: Rhea with Arav."""
import asyncio
import os

import httpx
import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from api.schemas import MachineHealth
from reliability import executor, ledger, policies, storage
from tests.fakes import FakeFactory, SimulatedCrash


# --- storage -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_indexes_is_idempotent(test_db):
    await storage.init_indexes()
    await storage.init_indexes()
    ledger_indexes = await storage.action_ledger().index_information()
    assert ledger_indexes["uniq_action_id"]["unique"]
    assert ledger_indexes["uniq_operation_id"]["unique"]


@pytest.mark.asyncio
async def test_enqueue_run_creates_queued_doc(test_db):
    run_id = await storage.enqueue_run("machine_b", crash_armed=True)
    assert run_id.startswith("run_")
    doc = await storage.agent_runs().find_one({"_id": run_id})
    assert doc["run_id"] == run_id
    assert doc["status"] == "QUEUED"
    assert doc["agent_id"] == "factory_recovery_agent"
    assert doc["machine_id"] == "machine_b"
    assert doc["crash_armed"] is True
    assert doc["crash_fired"] is False
    assert doc["restart_count"] == 0
    assert doc["fault_epoch"] is None
    assert doc["lease_owner"] is None
    assert doc["version"] == 1


@pytest.mark.asyncio
async def test_duplicate_operation_id_rejected(test_db):
    await storage.init_indexes()
    ledger = storage.action_ledger()
    await ledger.insert_one({"action_id": "act_1", "operation_id": "op_same"})
    with pytest.raises(DuplicateKeyError):
        await ledger.insert_one({"action_id": "act_2", "operation_id": "op_same"})


# --- ledger and policies -----------------------------------------------------

PROPOSAL = {
    "tool": "restart_sync",
    "machine_id": "machine_b",
    "procedure_id": "proc_sync_restart",
    "rationale": "sync process is down",
}
DENY_PROPOSAL = {**PROPOSAL, "tool": "shutdown_factory"}


@pytest.mark.asyncio
async def test_create_action_is_idempotent(test_db):
    await storage.init_indexes()
    first = await ledger.create_action("run_t1", PROPOSAL, fault_epoch=3)
    again = await ledger.create_action("run_t1", PROPOSAL, fault_epoch=3)
    assert first["action_id"] == again["action_id"]
    assert first["operation_id"] == "run_t1:machine_b:restart_sync:epoch_3"
    assert first["status"] == "PLANNED"
    # concurrent creators converge on one document
    a, b = await asyncio.gather(
        ledger.create_action("run_t2", PROPOSAL, 1),
        ledger.create_action("run_t2", PROPOSAL, 1),
    )
    assert a["action_id"] == b["action_id"]
    assert await storage.action_ledger().count_documents({"run_id": "run_t1"}) == 1
    assert await storage.action_ledger().count_documents({"run_id": "run_t2"}) == 1


@pytest.mark.asyncio
async def test_claim_action_atomically_only_once(test_db):
    await storage.init_indexes()
    action = await ledger.create_action("run_t3", PROPOSAL, 1)
    await ledger.mark_authorized(action["action_id"], {"allowed": True, "policy_version": 1, "reason": "ok"})
    assert await ledger.claim_action_atomically(action["action_id"], "worker_a") is True
    assert await ledger.claim_action_atomically(action["action_id"], "worker_b") is False
    doc = await ledger.get_action(action["action_id"])
    assert doc["status"] == "EXECUTING"
    assert doc["attempt_count"] == 1


@pytest.mark.asyncio
async def test_authorize_allows_restart_sync_on_machine_b(test_db):
    await storage.init_indexes()
    await policies.seed_default_policy()
    await policies.seed_default_policy()  # idempotent
    assert await storage.policies().count_documents({}) == 1
    decision = await policies.authorize("run_t4", "act_x", PROPOSAL)
    assert decision.allowed is True
    assert decision.policy_version == 1


@pytest.mark.asyncio
async def test_authorize_denies_shutdown_and_audits(test_db):
    await storage.init_indexes()
    await policies.seed_default_policy()
    action = await ledger.create_action("run_t5", DENY_PROPOSAL, 1)
    decision = await policies.authorize("run_t5", action["action_id"], DENY_PROPOSAL)
    assert decision.allowed is False
    event = await storage.audit_events().find_one(
        {"run_id": "run_t5", "event_type": "POLICY_CHECK", "decision": "DENIED"}
    )
    assert event is not None
    assert event["policy_version"] == 1
    assert event["reason"]
    # denied action: status DENIED, no execution receipt
    await ledger.mark_denied(action["action_id"], decision.reason, decision.policy_version)
    doc = await ledger.get_action(action["action_id"])
    assert doc["status"] == "DENIED"
    assert doc["external_receipt"] is None
    assert doc["attempt_count"] == 0
    # and a denied action can never be claimed for execution
    assert await ledger.claim_action_atomically(action["action_id"], "w") is False


@pytest.mark.asyncio
async def test_resume_run_missing_raises_value_error(test_db):
    with pytest.raises(ValueError):
        await ledger.resume_run("run_does_not_exist")


@pytest.mark.asyncio
async def test_resume_run_is_noop_for_completed_run(test_db):
    run_id = await storage.enqueue_run("machine_b")
    await ledger.set_run_status(run_id, "COMPLETED")
    result = await ledger.resume_run(run_id)
    assert result == {"run_id": run_id, "status": "COMPLETED"}
    doc = await storage.agent_runs().find_one({"_id": run_id})
    assert doc["status"] == "COMPLETED"
    assert doc["restart_count"] == 0
    assert doc["lease_owner"] is None


# --- executor ----------------------------------------------------------------


async def _run_status(run_id):
    return (await ledger.get_run(run_id))["status"]


@pytest.mark.asyncio
async def test_executor_happy_path(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    result = await executor.execute_guarded_action(run_id, PROPOSAL, factory=factory, owner="w1")
    assert result["status"] == "EFFECT_VERIFIED"
    assert result["operation_id"] == f"{run_id}:machine_b:restart_sync:epoch_1"
    assert result["external_receipt"]["applied"] is True
    assert result["verification"]["effect_present"] is True
    assert await _run_status(run_id) == "COMPLETED"
    assert factory.restart_count == 1


@pytest.mark.asyncio
async def test_executor_denies_shutdown_factory(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    result = await executor.execute_guarded_action(run_id, DENY_PROPOSAL, factory=factory, owner="w1")
    assert factory.mutation_calls == []
    assert factory.receipts == {}
    assert result["status"] == "DENIED"
    assert result["external_receipt"] is None
    assert result["attempt_count"] == 0
    assert await _run_status(run_id) == "DENIED"
    denied = await storage.audit_events().find_one(
        {"run_id": run_id, "event_type": "POLICY_CHECK", "decision": "DENIED"}
    )
    assert denied is not None


@pytest.mark.asyncio
async def test_crash_after_effect_then_resume_does_not_repeat(ready, monkeypatch):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b", crash_armed=True)
    crashes = []

    def fake_crash():
        # crash_fired must already be durable in Atlas before the process dies
        sync_client = MongoClient(os.environ["MONGODB_URI"])
        try:
            doc = sync_client[os.environ["MONGODB_DB"]]["agent_runs"].find_one({"_id": run_id})
        finally:
            sync_client.close()
        assert doc["crash_fired"] is True
        crashes.append(run_id)
        raise SimulatedCrash()

    monkeypatch.setattr(executor, "_crash", fake_crash)

    with pytest.raises(SimulatedCrash):
        await executor.execute_guarded_action(run_id, PROPOSAL, factory=factory, owner="worker_a")

    # effect exists, ledger does not know it yet
    (action,) = await ledger.get_machine_ledger(run_id)
    assert action["status"] == "EXECUTING"
    assert action["verification"] is None
    assert factory.fault["machine_b"] == "NONE"
    assert factory.restart_count == 1
    assert await _run_status(run_id) == "RUNNING"

    # a new worker resumes
    result = await executor.reconcile_run(run_id, factory=factory, owner="worker_b")
    assert result["run_id"] == run_id
    assert result["status"] == "COMPLETED"
    (resumed,) = result["actions"]
    assert resumed["status"] == "EFFECT_VERIFIED"
    assert resumed["action_id"] == action["action_id"]
    assert resumed["operation_id"] == action["operation_id"]
    assert factory.restart_count == 1
    assert len(factory.mutation_calls) == 1  # restart never called twice
    assert crashes == [run_id]  # crash did not fire again


@pytest.mark.asyncio
async def test_crash_before_effect_retries_once_with_same_operation_id(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    # worker_a claimed the action and died before calling the factory
    await ledger.pin_fault_epoch(run_id, 1)
    action = await ledger.create_action(run_id, PROPOSAL, 1)
    await ledger.mark_authorized(action["action_id"], {"allowed": True, "policy_version": 1, "reason": "ok"})
    assert await ledger.claim_action_atomically(action["action_id"], "worker_a")
    assert factory.mutation_calls == []

    result = await executor.reconcile_run(run_id, factory=factory, owner="worker_b")
    assert result["status"] == "COMPLETED"
    (resumed,) = result["actions"]
    assert resumed["status"] == "EFFECT_VERIFIED"
    assert resumed["attempt_count"] == 2
    assert factory.mutation_calls == [("restart_sync", "machine_b", action["operation_id"])]
    assert factory.restart_count == 1

    # reconciling again changes nothing
    await executor.reconcile_run(run_id, factory=factory, owner="worker_c")
    assert len(factory.mutation_calls) == 1


@pytest.mark.asyncio
async def test_execute_again_does_not_call_factory_again(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    first = await executor.execute_guarded_action(run_id, PROPOSAL, factory=factory, owner="w1")
    factory.epoch["machine_b"] = 2  # even if the factory's epoch moves, the run's is pinned
    second = await executor.execute_guarded_action(run_id, PROPOSAL, factory=factory, owner="w2")
    assert second["action_id"] == first["action_id"]
    assert second["operation_id"] == first["operation_id"]
    assert second["status"] == "EFFECT_VERIFIED"
    assert len(factory.mutation_calls) == 1
    assert factory.restart_count == 1
    assert await storage.action_ledger().count_documents({"run_id": run_id}) == 1


@pytest.mark.asyncio
async def test_ambiguous_verify_requires_review_without_retry(ready):
    factory = FakeFactory()
    factory.verify_error = True
    run_id = await storage.enqueue_run("machine_b")
    result = await executor.execute_guarded_action(run_id, PROPOSAL, factory=factory, owner="w1")
    assert result["status"] == "REQUIRES_REVIEW"
    assert await _run_status(run_id) == "NEEDS_RECONCILIATION"
    assert len(factory.mutation_calls) == 1

    # reconcile re-verifies once, still cannot tell: run FAILED, tool never re-called
    outcome = await executor.reconcile_run(run_id, factory=factory, owner="w2")
    assert outcome["status"] == "FAILED"
    assert outcome["actions"][0]["status"] == "REQUIRES_REVIEW"
    assert len(factory.mutation_calls) == 1


# --- factory client ----------------------------------------------------------


def _client_with(handler, token=None):
    client = executor.FactoryClient("http://factory.test", token=token)
    client._http = httpx.AsyncClient(
        base_url="http://factory.test", transport=httpx.MockTransport(handler)
    )
    return client


@pytest.mark.asyncio
async def test_factory_client_retries_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(executor, "RETRY_503_BACKOFF", 0)
    seen = []

    def handler(request):
        seen.append(request.read())
        if len(seen) <= 2:
            return httpx.Response(503, json={"detail": "retry"})
        return httpx.Response(200, json={"applied": True, "replayed": False})

    client = _client_with(handler)
    result = await client.restart_sync("machine_b", "op_1")
    assert result["applied"] is True
    assert len(seen) == 3
    assert len(set(seen)) == 1  # every attempt carried the same operation_id body


@pytest.mark.asyncio
async def test_factory_client_gives_up_after_three_retries(monkeypatch):
    monkeypatch.setattr(executor, "RETRY_503_BACKOFF", 0)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    client = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.health("machine_b")
    assert len(calls) == 1 + executor.RETRY_503_MAX


@pytest.mark.asyncio
async def test_factory_client_does_not_retry_409_or_timeouts(monkeypatch):
    monkeypatch.setattr(executor, "RETRY_503_BACKOFF", 0)
    calls = []

    def conflict(request):
        calls.append("409")
        return httpx.Response(409, json={"detail": "operation_id reused"})

    with pytest.raises(httpx.HTTPStatusError) as err:
        await _client_with(conflict).restart_sync("machine_b", "op_1")
    assert err.value.response.status_code == 409
    assert calls == ["409"]

    def timeout(request):
        calls.append("timeout")
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(httpx.ReadTimeout):
        await _client_with(timeout).restart_sync("machine_b", "op_1")
    assert calls == ["409", "timeout"]


def test_machine_health_parses_factory_payload():
    health = MachineHealth.model_validate(
        {
            "machine_id": "machine_b",
            "healthy": False,
            "fault_type": "SYNC_PROCESS_DOWN",
            "sync_running": False,
            "network_connected": True,
            "backlog": 125,
            "fault_epoch": 3,
            "last_operation_id": None,
            "restart_count": 2,
            "reconnect_count": 1,
            "message": "Sync process is not running.",
            "some_future_field": "ignored",
        }
    )
    assert health.restart_count == 2
    assert health.reconnect_count == 1
    assert health.message == "Sync process is not running."
    assert not hasattr(health, "some_future_field")
    minimal = MachineHealth.model_validate(
        {k: v for k, v in health.model_dump().items()
         if k not in ("restart_count", "reconnect_count", "message")}
    )
    assert (minimal.restart_count, minimal.reconnect_count, minimal.message) == (0, 0, None)


def test_session_database_is_random_and_never_shared(test_db_name):
    import re

    assert re.fullmatch(r"ghostshift_test_[0-9a-f]{8}", test_db_name)
    assert test_db_name not in ("ghostshift_test_rhea", "ghostshift_hackathon", "ghostshift")
    assert os.environ["MONGODB_DB"] == test_db_name
