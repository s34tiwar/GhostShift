"""Ledger, policy, crash, and reconciliation tests. Owner: Rhea with Arav."""
import pytest
import pytest_asyncio
from pymongo.errors import DuplicateKeyError

import asyncio
import os

from pymongo import MongoClient

from reliability import executor, ledger, policies, storage

TEST_DB = "ghostshift_test_rhea"
TEST_COLLECTIONS = ("agent_runs", "action_ledger", "policies", "audit_events")


# --- storage -----------------------------------------------------------------


@pytest_asyncio.fixture
async def test_db(monkeypatch):
    """Point storage at the private test DB (never ghostshift_hackathon)."""
    monkeypatch.setenv("MONGODB_DB", TEST_DB)
    await storage.close_client()  # fresh client bound to this test's event loop
    db = storage.get_db()
    assert db.name == TEST_DB
    yield db
    for name in TEST_COLLECTIONS:
        await db.drop_collection(name)
    await storage.close_client()


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


class SimulatedCrash(Exception):
    """Stands in for os._exit(42) so the test process survives."""


class FakeFactory:
    """In-memory factory: receipts keyed by operation_id, like the real one."""

    def __init__(self):
        self.fault = {"machine_a": "NONE", "machine_b": "SYNC_PROCESS_DOWN", "machine_c": "NONE"}
        self.epoch = {"machine_a": 1, "machine_b": 1, "machine_c": 1}
        self.receipts = {}
        self.restart_count = 0
        self.mutation_calls = []  # (tool, machine_id, operation_id)
        self.verify_error = False

    async def health(self, machine_id):
        fault = self.fault[machine_id]
        return {
            "machine_id": machine_id,
            "healthy": fault == "NONE",
            "fault_type": fault,
            "sync_running": fault != "SYNC_PROCESS_DOWN",
            "network_connected": fault != "NETWORK_DISCONNECTED",
            "backlog": 0,
            "fault_epoch": self.epoch[machine_id],
            "last_operation_id": None,
        }

    def _apply(self, tool, fixes, machine_id, operation_id):
        self.mutation_calls.append((tool, machine_id, operation_id))
        replayed = operation_id in self.receipts
        if not replayed:
            self.receipts[operation_id] = tool
            if tool == "restart_sync":
                self.restart_count += 1
            if self.fault[machine_id] == fixes:
                self.fault[machine_id] = "NONE"
        return {
            "operation_id": operation_id,
            "applied": not replayed,
            "replayed": replayed,
            "effect_present": self.fault[machine_id] == "NONE",
        }

    async def restart_sync(self, machine_id, operation_id):
        return self._apply("restart_sync", "SYNC_PROCESS_DOWN", machine_id, operation_id)

    async def reconnect(self, machine_id, operation_id):
        return self._apply("reconnect_network", "NETWORK_DISCONNECTED", machine_id, operation_id)

    async def verify(self, machine_id, expected_action, operation_id):
        if self.verify_error:
            raise RuntimeError("verify unavailable")
        return {
            "effect_present": self.fault[machine_id] == "NONE",
            "receipt_exists": operation_id in self.receipts,
        }


@pytest_asyncio.fixture
async def ready(test_db):
    await storage.init_indexes()
    await policies.seed_default_policy()
    return test_db


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
            doc = sync_client[TEST_DB]["agent_runs"].find_one({"_id": run_id})
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
