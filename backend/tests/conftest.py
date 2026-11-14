"""Shared fixtures for Rhea's reliability tests."""
import pytest_asyncio

from reliability import policies, storage
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
