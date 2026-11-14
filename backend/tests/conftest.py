"""Shared fixtures for Rhea's reliability tests."""
import sys
from pathlib import Path

import pytest_asyncio

# Make `reliability`, `api`, `agent` and `tests` importable whether pytest is run
# from the repo root or from backend/.
_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from reliability import policies, storage  # noqa: E402
from tests.fakes import TEST_COLLECTIONS, TEST_DB


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


@pytest_asyncio.fixture
async def ready(test_db):
    await storage.init_indexes()
    await policies.seed_default_policy()
    return test_db
