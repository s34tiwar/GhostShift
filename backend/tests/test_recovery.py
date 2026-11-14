"""Ledger, policy, crash, and reconciliation tests. Owner: Rhea with Arav."""
import pytest
import pytest_asyncio
from pymongo.errors import DuplicateKeyError

from reliability import storage

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
