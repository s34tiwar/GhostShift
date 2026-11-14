"""Shared test fixtures.

- Rhea's reliability tests import `reliability`, `api`, `tests.fakes` (backend/ on sys.path).
- Arav's factory tests run against a REAL MongoDB (TEST_MONGODB_URI), each test in its own
  throwaway collection that is dropped afterwards. Skipped if MongoDB is unreachable.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR)):  # works whether pytest runs from repo root or backend/
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_dotenv() -> None:
    """Fill missing env vars from the repo's .env (no extra dependency)."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'").strip('"')
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

from reliability import policies, storage  # noqa: E402
from tests.fakes import TEST_COLLECTIONS, TEST_DB_PREFIX  # noqa: E402


# ---------------- Rhea: reliability fixtures ----------------
def _drop_test_database(name: str) -> None:
    """Drop this session's whole database; fall back to the four collections.

    Uses a sync client (no event loop at session end). Never prints the URI.
    """
    import warnings

    from pymongo import MongoClient
    from pymongo.errors import OperationFailure

    uri = os.environ.get("BACKEND_MONGODB_URI") or os.environ.get("MONGODB_URI")
    # Refuse to drop anything that is not a random per-session test database.
    if not uri or not name.startswith(TEST_DB_PREFIX):
        return
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        try:
            client.drop_database(name)
        except OperationFailure:  # Atlas user may lack dropDatabase
            for collection in TEST_COLLECTIONS:
                client[name].drop_collection(collection)
    except Exception as exc:  # cleanup must never fail the run
        warnings.warn(f"could not clean up test database {name}: {type(exc).__name__}")
    finally:
        client.close()


@pytest.fixture(scope="session")
def test_db_name():
    """A random `ghostshift_test_<8 hex>` database for this pytest session only.

    Two people (or two runs) on the same Atlas cluster never share data, so one
    session's worker can never lease another session's runs.
    """
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:8]}"
    previous = os.environ.get("MONGODB_DB")
    os.environ["MONGODB_DB"] = name
    try:
        yield name
    finally:
        _drop_test_database(name)
        if previous is None:
            os.environ.pop("MONGODB_DB", None)
        else:
            os.environ["MONGODB_DB"] = previous


@pytest_asyncio.fixture
async def test_db(test_db_name, monkeypatch):
    """Point storage at this session's private database (never ghostshift_hackathon)."""
    monkeypatch.setenv("MONGODB_DB", test_db_name)
    await storage.close_client()  # fresh client bound to this test's event loop
    db = storage.get_db()
    assert db.name == test_db_name and db.name.startswith(TEST_DB_PREFIX)
    yield db
    for name in TEST_COLLECTIONS:  # per-test isolation inside the session
        await db.drop_collection(name)
    await storage.close_client()


@pytest_asyncio.fixture
async def ready(test_db):
    await storage.init_indexes()
    await policies.seed_default_policy()
    return test_db


# ---------------- Arav: factory fixtures ----------------
FACTORY_TEST_DB_NAME = os.environ.get("TEST_MONGODB_DB") or "ghostshift_factory_tests"


@pytest.fixture(scope="session")
def mongo_client():
    from pymongo import MongoClient

    uri = os.environ.get("TEST_MONGODB_URI", "").strip()
    if not uri:
        pytest.skip("TEST_MONGODB_URI not set; factory tests need a real MongoDB")
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB unreachable ({type(exc).__name__}); check Atlas Network Access / credentials")
    yield client
    client.close()


@pytest.fixture
def factory_db(mongo_client):
    return mongo_client[FACTORY_TEST_DB_NAME]


@pytest.fixture
def collection(factory_db):
    name = f"factory_test_{uuid.uuid4().hex[:12]}"
    col = factory_db[name]
    yield col
    col.drop()


@pytest.fixture
def factory_client(collection):
    from fastapi.testclient import TestClient

    from backend.factory.main import create_app
    from backend.factory.mongo_store import FactoryStore

    app = create_app(lambda: FactoryStore(collection))
    with TestClient(app) as client:
        yield client
