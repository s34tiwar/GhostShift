"""Durable run, action-ledger, and audit transitions. Owner: Rhea."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from api.schemas import RunView, make_operation_id, new_action_id
from reliability import storage

CRASH_STAGE = "after_effect_before_verify"
TERMINAL_RUN_STATUSES = ("COMPLETED", "DENIED")  # resume_run is a no-op for these


def _dump(value):
    """Accept a pydantic model or a plain dict."""
    return value.model_dump() if hasattr(value, "model_dump") else dict(value)


def _strip_id(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


# --- audit -------------------------------------------------------------------


async def append_audit(
    run_id, action_id, actor, event_type, decision, reason, detail, policy_version
) -> str:
    """Insert one audit event and return its event_id."""
    event_id = uuid4().hex
    await storage.audit_events().insert_one(
        {
            "event_id": event_id,
            "run_id": run_id,
            "action_id": action_id,
            "actor": actor,
            "event_type": event_type,
            "decision": decision,
            "reason": reason,
            "detail": detail,
            "policy_version": policy_version,
            "at": storage.utc_now(),
        }
    )
    return event_id


async def list_audit(run_id: str) -> dict:
    cursor = storage.audit_events().find({"run_id": run_id}).sort(
        [("at", ASCENDING), ("_id", ASCENDING)]
    )
    events = [
        {
            "at": d["at"],
            "actor": d["actor"],
            "event_type": d["event_type"],
            "reason": d.get("reason"),
            "action_id": d.get("action_id"),
            "policy_version": d.get("policy_version"),
        }
        async for d in cursor
    ]
    return {"events": events}


# --- actions -----------------------------------------------------------------


async def get_action(action_id: str) -> dict | None:
    return await storage.action_ledger().find_one({"action_id": action_id})


async def create_action(run_id: str, proposal: dict, fault_epoch: int) -> dict:
    """Idempotent: the same (run, machine, tool, epoch) always yields one doc."""
    machine_id = proposal["machine_id"]
    tool = proposal["tool"]
    operation_id = make_operation_id(run_id, machine_id, tool, fault_epoch)
    ledger = storage.action_ledger()

    existing = await ledger.find_one({"operation_id": operation_id})
    if existing is not None:
        return existing

    action_id = new_action_id()
    doc = {
        "_id": action_id,
        "action_id": action_id,
        "run_id": run_id,
        "operation_id": operation_id,
        "agent_id": storage.AGENT_ID,
        "target": machine_id,
        "action_type": tool,
        "status": "PLANNED",
        "policy_decision": None,
        "attempt_count": 0,
        "external_receipt": None,
        "verification": None,
        "procedure_id": proposal.get("procedure_id"),
        "rationale": proposal.get("rationale"),
        "proposal": dict(proposal),
        "fault_epoch": fault_epoch,
        "created_at": storage.utc_now(),
        "started_at": None,
        "verified_at": None,
    }
    try:
        await ledger.insert_one(doc)
    except DuplicateKeyError:
        # Lost a race with another creator: return the winner's doc.
        return await ledger.find_one({"operation_id": operation_id})
    return doc


async def _transition(action_id: str, from_statuses, fields: dict) -> dict | None:
    """Apply fields only if the action is in one of from_statuses.

    Returns the updated doc, or the unchanged current doc if the guard failed.
    """
    doc = await storage.action_ledger().find_one_and_update(
        {"action_id": action_id, "status": {"$in": list(from_statuses)}},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    return doc if doc is not None else await get_action(action_id)


async def mark_authorized(action_id: str, decision) -> dict | None:
    return await _transition(
        action_id,
        ("PLANNED", "AUTHORIZED"),
        {"status": "AUTHORIZED", "policy_decision": _dump(decision)},
    )


async def mark_denied(action_id: str, reason: str, policy_version: int | None = None) -> dict | None:
    return await _transition(
        action_id,
        ("PLANNED", "AUTHORIZED"),
        {
            "status": "DENIED",
            "policy_decision": {
                "allowed": False,
                "policy_version": policy_version,
                "reason": reason,
            },
        },
    )


async def claim_action_atomically(action_id: str, owner: str) -> bool:
    """AUTHORIZED -> EXECUTING in one atomic step. True only for the winner."""
    doc = await storage.action_ledger().find_one_and_update(
        {"action_id": action_id, "status": "AUTHORIZED"},
        {
            "$set": {
                "status": "EXECUTING",
                "started_at": storage.utc_now(),
                "claimed_by": owner,
            },
            "$inc": {"attempt_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None


async def mark_effect_verified(action_id: str, actual: dict, external_receipt=None) -> dict | None:
    now = storage.utc_now()
    fields = {
        "status": "EFFECT_VERIFIED",
        "verification": {"effect_present": True, "checked_at": now},
        "verified_at": now,
    }
    if external_receipt is not None:
        fields["external_receipt"] = external_receipt
    return await _transition(action_id, ("EXECUTING", "REQUIRES_REVIEW"), fields)


async def mark_requires_review(action_id: str, actual: dict, external_receipt=None) -> dict | None:
    fields = {
        "status": "REQUIRES_REVIEW",
        "verification": {
            "effect_present": bool(actual.get("effect_present", False)),
            "checked_at": storage.utc_now(),
        },
    }
    if external_receipt is not None:
        fields["external_receipt"] = external_receipt
    return await _transition(action_id, ("EXECUTING", "AUTHORIZED"), fields)


async def get_machine_ledger(run_id: str) -> list[dict]:
    cursor = storage.action_ledger().find({"run_id": run_id}).sort("created_at", ASCENDING)
    return [_strip_id(d) async for d in cursor]


# --- runs --------------------------------------------------------------------


def _worker_status(doc: dict) -> str:
    expires = doc.get("lease_expires_at")
    if doc.get("lease_owner") and expires:
        if datetime.fromisoformat(expires) > datetime.now(timezone.utc):
            return "ACTIVE"
    return "IDLE"


async def get_run(run_id: str) -> dict | None:
    doc = await storage.agent_runs().find_one({"_id": run_id})
    if doc is None:
        return None
    return RunView(
        run_id=doc["run_id"],
        status=doc["status"],
        machine_id=doc["machine_id"],
        procedure_id=doc.get("procedure_id"),
        retrieval_mode=doc.get("retrieval_mode"),
        current_action_id=doc.get("current_action_id"),
        restart_count=doc.get("restart_count", 0),
        worker_status=_worker_status(doc),
    ).model_dump()


async def has_active_runs() -> bool:
    """Prevent a demo reset while any worker may still be repairing a machine."""
    return await storage.agent_runs().find_one(
        {"status": {"$nin": ["COMPLETED", "FAILED", "DENIED"]}},
        {"_id": 1},
    ) is not None


async def set_run_status(run_id: str, status: str, **fields) -> dict:
    doc = await storage.agent_runs().find_one_and_update(
        {"_id": run_id},
        {
            "$set": {**fields, "status": status, "updated_at": storage.utc_now()},
            "$inc": {"version": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise ValueError(f"run {run_id} does not exist")
    return doc


async def arm_crash(run_id: str, stage: str) -> dict:
    if stage != CRASH_STAGE:
        raise ValueError(f"unsupported crash stage: {stage}")
    result = await storage.agent_runs().update_one(
        {"_id": run_id},
        {
            "$set": {
                "crash_armed": True,
                "crash_stage": stage,
                "updated_at": storage.utc_now(),
            },
            "$inc": {"version": 1},
        },
    )
    if result.matched_count == 0:
        raise ValueError(f"run {run_id} does not exist")
    return {"armed": True}


async def resume_run(run_id: str) -> dict:
    """Release the unfinished run for reconciliation. Never mints a new run_id."""
    doc = await storage.agent_runs().find_one_and_update(
        {"_id": run_id, "status": {"$nin": list(TERMINAL_RUN_STATUSES)}},
        {
            "$set": {
                "status": "NEEDS_RECONCILIATION",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": storage.utc_now(),
            },
            "$inc": {"restart_count": 1, "version": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        # Either missing, or COMPLETED/DENIED (resume is a no-op for those).
        current = await storage.agent_runs().find_one({"_id": run_id})
        if current is None:
            raise ValueError(f"run {run_id} does not exist")
        return {"run_id": run_id, "status": current["status"]}
    return {"run_id": run_id, "status": "NEEDS_RECONCILIATION"}


# --- executor helpers --------------------------------------------------------


async def reclaim_stale_action(action_id: str, owner: str) -> bool:
    """Take over an action for one same-key retry. True only for the winner.

    Matches an EXECUTING action claimed by a different owner (its worker died),
    or an AUTHORIZED action that was never claimed.
    """
    doc = await storage.action_ledger().find_one_and_update(
        {
            "action_id": action_id,
            "$or": [
                {"status": "EXECUTING", "claimed_by": {"$ne": owner}},
                {"status": "AUTHORIZED"},
            ],
        },
        [
            {
                "$set": {
                    "status": "EXECUTING",
                    "claimed_by": owner,
                    "started_at": {"$ifNull": ["$started_at", storage.utc_now()]},
                    "attempt_count": {"$add": ["$attempt_count", 1]},
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None


async def save_receipt(action_id: str, receipt: dict) -> None:
    """Record the factory's receipt. Does not change status: a receipt is not proof."""
    await storage.action_ledger().update_one(
        {"action_id": action_id, "status": "EXECUTING"},
        {"$set": {"external_receipt": receipt}},
    )


async def fire_crash_once(run_id: str) -> bool:
    """Atomically flip crash_fired. True for exactly one caller per armed run."""
    doc = await storage.agent_runs().find_one_and_update(
        {"_id": run_id, "crash_armed": True, "crash_fired": False},
        {"$set": {"crash_fired": True, "updated_at": storage.utc_now()}},
    )
    return doc is not None


async def get_fault_epoch(run_id: str) -> int | None:
    """The fault epoch pinned on the run, or None if not pinned yet."""
    doc = await storage.agent_runs().find_one({"_id": run_id}, {"fault_epoch": 1})
    if doc is None:
        raise ValueError(f"run {run_id} does not exist")
    return doc.get("fault_epoch")


async def set_run_fields(run_id: str, **fields) -> None:
    """Set fields on a run without changing its status."""
    await storage.agent_runs().update_one(
        {"_id": run_id},
        {"$set": {**fields, "updated_at": storage.utc_now()}, "$inc": {"version": 1}},
    )


# --- run leases (worker) -------------------------------------------------------

LEASABLE_RUN_STATUSES = (
    "QUEUED",
    "NEEDS_RECONCILIATION",
    "DIAGNOSING",
    "ACTION_PENDING",
    "RUNNING",
)


def lease_time(offset_seconds: float = 0.0) -> str:
    """Fixed-width UTC ISO-8601 (always microseconds) so string order == time order."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return moment.isoformat(timespec="microseconds")


async def acquire_run_lease(owner: str, lease_seconds: float) -> dict | None:
    """Atomically lease the oldest eligible run. None if nothing is available.

    Eligible: a non-terminal status and no live lease (owner unset or expired).
    recovery_attempts counts every pickup after the first one.
    """
    now = lease_time()
    return await storage.agent_runs().find_one_and_update(
        {
            "status": {"$in": list(LEASABLE_RUN_STATUSES)},
            "$or": [
                {"lease_owner": None},
                {"lease_expires_at": None},
                {"lease_expires_at": {"$lt": now}},
            ],
        },
        [
            {
                "$set": {
                    "recovery_attempts": {
                        "$add": [
                            {"$ifNull": ["$recovery_attempts", 0]},
                            {"$cond": [{"$ifNull": ["$first_picked_at", False]}, 1, 0]},
                        ]
                    },
                    "first_picked_at": {"$ifNull": ["$first_picked_at", now]},
                    "lease_owner": owner,
                    "lease_expires_at": lease_time(lease_seconds),
                    "updated_at": now,
                }
            }
        ],
        sort=[("updated_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


async def renew_lease(run_id: str, owner: str, lease_seconds: float) -> bool:
    """Extend the lease only if this owner still holds it."""
    result = await storage.agent_runs().update_one(
        {"_id": run_id, "lease_owner": owner},
        {"$set": {"lease_expires_at": lease_time(lease_seconds)}},
    )
    return result.matched_count == 1


async def release_lease(run_id: str, owner: str) -> bool:
    result = await storage.agent_runs().update_one(
        {"_id": run_id, "lease_owner": owner},
        {"$set": {"lease_owner": None, "lease_expires_at": None}},
    )
    return result.matched_count == 1


async def pin_fault_epoch(run_id: str, epoch: int) -> int:
    """Pin the run's fault epoch once; later calls return the pinned value."""
    await storage.agent_runs().update_one(
        {"_id": run_id, "fault_epoch": None}, {"$set": {"fault_epoch": epoch}}
    )
    pinned = await get_fault_epoch(run_id)
    return pinned
