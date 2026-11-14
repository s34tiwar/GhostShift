"""Async MongoDB connection, collections, and indexes. Owner: Rhea."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import ASCENDING, AsyncMongoClient

from api.schemas import new_run_id

# Does not override variables that are already set (e.g. a test override).
load_dotenv()

AGENT_ID = "factory_recovery_agent"
ACTIVE_RUN_STATUSES = ("QUEUED", "CREATED", "DIAGNOSING", "ACTION_PENDING", "RUNNING", "NEEDS_RECONCILIATION")


class ActiveRunExistsError(Exception):
    """A machine already has an unfinished recovery run."""

# One client per process / event loop, created lazily. The worker and the API
# are separate processes, so each builds its own. Never share across loops.
_client: AsyncMongoClient | None = None


def utc_now() -> str:
    """Current time as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        uri = os.environ.get("BACKEND_MONGODB_URI") or os.environ.get("MONGODB_URI")
        if not uri:
            raise RuntimeError(
                "BACKEND_MONGODB_URI is not set (legacy MONGODB_URI is also accepted)"
            )
        _client = AsyncMongoClient(uri)
    return _client


def get_db():
    name = os.environ.get("MONGODB_DB")
    if not name:
        raise RuntimeError("MONGODB_DB is not set")
    return get_client()[name]


async def close_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.close()


def agent_runs():
    return get_db()["agent_runs"]


def action_ledger():
    return get_db()["action_ledger"]


def policies():
    return get_db()["policies"]


def audit_events():
    return get_db()["audit_events"]


async def init_indexes() -> None:
    """Create all indexes. Idempotent: safe to call on every startup."""
    await agent_runs().create_index(
        [("status", ASCENDING), ("updated_at", ASCENDING)], name="status_updated_at"
    )
    await agent_runs().create_index(
        [("machine_id", ASCENDING), ("created_at", ASCENDING)], name="machine_created_at"
    )
    await action_ledger().create_index("action_id", unique=True, name="uniq_action_id")
    await action_ledger().create_index("operation_id", unique=True, name="uniq_operation_id")
    await action_ledger().create_index(
        [("run_id", ASCENDING), ("created_at", ASCENDING)], name="run_created_at"
    )
    await policies().create_index(
        [("agent_id", ASCENDING), ("policy_version", ASCENDING)],
        unique=True,
        name="uniq_agent_policy_version",
    )
    await audit_events().create_index("event_id", unique=True, name="uniq_event_id")
    await audit_events().create_index(
        [("run_id", ASCENDING), ("at", ASCENDING)], name="run_at"
    )


KNOWN_SCENARIOS = (None, "unsafe_shutdown")


async def enqueue_run(
    machine_id: str, crash_armed: bool = False, scenario: str | None = None
) -> str:
    """Insert a QUEUED run and return its run_id."""
    if scenario not in KNOWN_SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario!r}")
    existing = await agent_runs().find_one(
        {"machine_id": machine_id, "status": {"$in": list(ACTIVE_RUN_STATUSES)}},
        {"_id": 1},
    )
    if existing is not None:
        raise ActiveRunExistsError(f"{machine_id} already has an active run")
    run_id = new_run_id()
    now = utc_now()
    await agent_runs().insert_one(
        {
            "_id": run_id,
            "run_id": run_id,
            "agent_id": AGENT_ID,
            "machine_id": machine_id,
            "fault_epoch": None,
            "status": "QUEUED",
            "current_action_id": None,
            "last_verified_step": None,
            "crash_armed": crash_armed,
            "crash_fired": False,
            "scenario": scenario,
            "restart_count": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
    )
    return run_id
