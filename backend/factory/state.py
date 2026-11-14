"""Factory machine and fault state backed by MongoDB. Owner: Arav.

Pure logic only (no I/O): machine defaults, fault rules, repair outcomes and the
public health / verify views. mongo_store.py does the persistence.
"""

from __future__ import annotations

from typing import Any

MACHINE_IDS: tuple[str, ...] = ("machine_a", "machine_b", "machine_c")

FAULT_NONE = "NONE"
FAULT_SYNC = "SYNC_PROCESS_DOWN"
FAULT_NETWORK = "NETWORK_DISCONNECTED"
FAULT_TYPES: tuple[str, ...] = (FAULT_NONE, FAULT_SYNC, FAULT_NETWORK)
BREAKABLE_FAULTS: tuple[str, ...] = (FAULT_SYNC, FAULT_NETWORK)

ACTION_RESTART_SYNC = "restart_sync"
ACTION_RECONNECT = "reconnect"
ACTIONS: tuple[str, ...] = (ACTION_RESTART_SYNC, ACTION_RECONNECT)

# Which fault each repair action fixes.
ACTION_FIXES: dict[str, str] = {
    ACTION_RESTART_SYNC: FAULT_SYNC,
    ACTION_RECONNECT: FAULT_NETWORK,
}

FAULT_BACKLOG = 125
MAX_RECEIPTS = 200  # receipts kept per machine document (bounded doc size)

# Stable strings: Shivani's vector query text uses these. Do not reword casually.
MESSAGES: dict[str, str] = {
    FAULT_NONE: "Machine is healthy. Sync process running and network connected.",
    FAULT_SYNC: "Sync process is not running. Production data is not being uploaded and the backlog is growing.",
    FAULT_NETWORK: "Network connection lost. The machine cannot reach the data collector and the backlog is growing.",
}

# Fields that make up the live machine state (everything except bookkeeping).
STATE_FIELDS = (
    "healthy",
    "fault_type",
    "sync_running",
    "network_connected",
    "backlog",
    "fault_epoch",
    "last_operation_id",
    "restart_count",
    "reconnect_count",
    "message",
)


def healthy_state() -> dict[str, Any]:
    """State of a freshly created / reset machine."""
    return {
        "healthy": True,
        "fault_type": FAULT_NONE,
        "sync_running": True,
        "network_connected": True,
        "backlog": 0,
        "fault_epoch": 0,
        "last_operation_id": None,
        "restart_count": 0,
        "reconnect_count": 0,
        "message": MESSAGES[FAULT_NONE],
    }


def broken_state_changes(fault_type: str) -> dict[str, Any]:
    """Fields to $set when a fault is injected (fault_epoch is $inc'd separately)."""
    return {
        "healthy": False,
        "fault_type": fault_type,
        "sync_running": fault_type != FAULT_SYNC,
        "network_connected": fault_type != FAULT_NETWORK,
        "backlog": FAULT_BACKLOG,
        "message": MESSAGES[fault_type],
    }


def state_effect_present(doc: dict[str, Any], action: str) -> bool:
    """Does the live state show the effect of `action`?"""
    if action == ACTION_RESTART_SYNC:
        return bool(doc["sync_running"]) and doc["fault_type"] != FAULT_SYNC
    return bool(doc["network_connected"]) and doc["fault_type"] != FAULT_NETWORK


def plan_repair(doc: dict[str, Any], action: str, operation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decide the outcome of a NEW repair operation.

    Returns (state_changes_to_set, receipt). state_changes is empty when nothing
    is applied (healthy machine or wrong repair for the fault).
    """
    fault = doc["fault_type"]
    changes: dict[str, Any] = {}
    if fault == FAULT_NONE:
        applied, reason = False, "already_healthy"
    elif ACTION_FIXES[action] != fault:
        applied, reason = False, "wrong_action_for_fault"
    else:
        applied, reason = True, "applied"
        counter = "restart_count" if action == ACTION_RESTART_SYNC else "reconnect_count"
        changes = {
            "healthy": True,
            "fault_type": FAULT_NONE,
            "sync_running": True,
            "network_connected": True,
            "backlog": 0,
            "message": MESSAGES[FAULT_NONE],
            "last_operation_id": operation_id,
            counter: int(doc[counter]) + 1,
        }
    receipt = {
        "operation_id": operation_id,
        "action_type": action,
        "applied": applied,
        "reason": reason,
        "fault_epoch": int(doc["fault_epoch"]),
        "fault_type_before": fault,
    }
    return changes, receipt


def find_receipt(doc: dict[str, Any], operation_id: str | None) -> dict[str, Any] | None:
    if not operation_id:
        return None
    for receipt in doc.get("receipts", []):
        if receipt["operation_id"] == operation_id:
            return receipt
    return None


def health_view(doc: dict[str, Any]) -> dict[str, Any]:
    view = {"machine_id": doc["_id"]}
    for field in STATE_FIELDS:
        view[field] = doc[field]
    view["healthy"] = bool(view["healthy"])
    view["sync_running"] = bool(view["sync_running"])
    view["network_connected"] = bool(view["network_connected"])
    return view


def repair_result(doc: dict[str, Any], receipt: dict[str, Any], replayed: bool) -> dict[str, Any]:
    """Response for POST restart-sync / reconnect. `doc` is the state AFTER the op."""
    return {
        "operation_id": receipt["operation_id"],
        "machine_id": doc["_id"],
        "action_type": receipt["action_type"],
        "applied": bool(receipt["applied"]),
        "replayed": replayed,
        "effect_present": state_effect_present(doc, receipt["action_type"]),
        "reason": receipt["reason"],
        "fault_epoch": int(doc["fault_epoch"]),
        "fault_type": doc["fault_type"],
        "healthy": bool(doc["healthy"]),
        "restart_count": int(doc["restart_count"]),
        "reconnect_count": int(doc["reconnect_count"]),
    }


def verify_view(doc: dict[str, Any], expected_action: str, operation_id: str | None) -> dict[str, Any]:
    """Independent verification: effect_present only if an APPLIED receipt for the
    CURRENT fault_epoch exists for this action AND the live state shows the repair."""
    op = operation_id or doc.get("last_operation_id")
    receipt = find_receipt(doc, op)
    receipt_exists = receipt is not None
    receipt_applied = bool(receipt and receipt["applied"] and receipt["action_type"] == expected_action)
    receipt_epoch = int(receipt["fault_epoch"]) if receipt else None
    state_ok = state_effect_present(doc, expected_action)
    effect = receipt_applied and receipt_epoch == int(doc["fault_epoch"]) and state_ok
    return {
        "machine_id": doc["_id"],
        "effect_present": effect,
        "receipt_exists": receipt_exists,
        "receipt_applied": receipt_applied,
        "receipt_epoch": receipt_epoch,
        "state_effect_present": state_ok,
        "operation_id": op,
        "healthy": bool(doc["healthy"]),
        "fault_type": doc["fault_type"],
        "fault_epoch": int(doc["fault_epoch"]),
        "restart_count": int(doc["restart_count"]),
        "reconnect_count": int(doc["reconnect_count"]),
        "expected_action": expected_action,
    }
