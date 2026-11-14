"""Shared fixtures. Factory tests run against a REAL MongoDB (TEST_MONGODB_URI), each test in
its own throwaway collection that is dropped afterwards. Skipped if MongoDB is unreachable."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


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

TEST_DB_NAME = os.environ.get("TEST_MONGODB_DB") or "ghostshift_factory_tests"


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
def test_db(mongo_client):
    return mongo_client[TEST_DB_NAME]


@pytest.fixture
def collection(test_db):
    name = f"factory_test_{uuid.uuid4().hex[:12]}"
    col = test_db[name]
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
