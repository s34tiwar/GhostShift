"""Ledger, policy, crash, and reconciliation tests. Owner: Rhea with Arav."""
import pytest
import pytest_asyncio
from pymongo.errors import DuplicateKeyError

import asyncio

from reliability import ledger, policies, storage

TEST_DB = "ghostshift_test_rhea"
TEST_COLLECTIONS = ("agent_runs", "action_ledger", "policies", "audit_events")


# --- storage -----------------------------------------------------------------


@pytest_asyncio.fixture
async def test_db(monkeypatch):
    """Point storage at the private test DB (never ghostshift_hackthon)."""
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
