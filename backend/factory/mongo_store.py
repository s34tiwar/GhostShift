"""MongoDB-backed machine state and idempotent operation receipts. Owner: Arav.

One document per machine in its own collection (default `factory_machines`):
    {_id: "machine_b", <state fields>, receipts: [...], version: <int>}

A repair is a single atomic find_one_and_update guarded by
  (version unchanged) AND (no receipt with this operation_id yet)
that sets the new state, appends the receipt and bumps `version`.
No multi-document transactions. Never touches other teams' collections.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from . import state

MAX_ATTEMPTS = 40


class UnknownMachine(Exception):
    pass


class OperationConflict(Exception):
    """operation_id already used for a different machine or action (HTTP 409)."""


class Contended(Exception):
    """Lost the compare-and-set race too many times (HTTP 503)."""


class FactoryStore:
    def __init__(self, collection: Collection):
        self.col = collection

    # ---------- construction ----------
    @classmethod
    def from_env(cls) -> "FactoryStore":
        uri = (
            os.environ.get("BACKEND_MONGODB_URI")
            or os.environ.get("MONGODB_URI", "")
        ).strip()
        if not uri:
            raise RuntimeError(
                "BACKEND_MONGODB_URI is not set; the factory needs MongoDB to start "
                "(legacy MONGODB_URI is also accepted)."
            )
        db_name = os.environ.get("MONGODB_DB", "").strip() or "ghostshift"
        col_name = os.environ.get("FACTORY_COLLECTION", "").strip() or "factory_machines"
        client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="ghostshift-factory")
        return cls(client[db_name][col_name])

    def ensure_machines(self) -> None:
        """Create missing machines. $setOnInsert: never resets an existing (broken) machine."""
        self.col.create_index("receipts.operation_id")
        for machine_id in state.MACHINE_IDS:
            try:
                self.col.update_one(
                    {"_id": machine_id},
                    {"$setOnInsert": {**state.healthy_state(), "receipts": [], "version": 0}},
                    upsert=True,
                )
            except DuplicateKeyError:
                pass  # another factory instance inserted it concurrently

    # ---------- reads ----------
    def _get(self, machine_id: str) -> dict[str, Any]:
        if machine_id not in state.MACHINE_IDS:
            raise UnknownMachine(machine_id)
        doc = self.col.find_one({"_id": machine_id})
        if doc is None:  # collection wiped underneath us: recreate defaults
            self.ensure_machines()
            doc = self.col.find_one({"_id": machine_id})
        return doc

    def list_machines(self) -> list[dict[str, Any]]:
        return [state.health_view(self._get(m)) for m in state.MACHINE_IDS]

    def health(self, machine_id: str) -> dict[str, Any]:
        return state.health_view(self._get(machine_id))

    def verify(self, machine_id: str, expected_action: str, operation_id: str | None) -> dict[str, Any]:
        return state.verify_view(self._get(machine_id), expected_action, operation_id)

    # ---------- writes ----------
    def break_machine(self, machine_id: str, fault_type: str) -> dict[str, Any]:
        self._get(machine_id)
        doc = self.col.find_one_and_update(
            {"_id": machine_id},
            {"$set": state.broken_state_changes(fault_type), "$inc": {"fault_epoch": 1, "version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        return state.health_view(doc)

    def repair(self, machine_id: str, action: str, operation_id: str) -> dict[str, Any]:
        checked_other_machines = False
        for attempt in range(MAX_ATTEMPTS):
            doc = self._get(machine_id)
            existing = state.find_receipt(doc, operation_id)
            if existing is not None:
                if existing["action_type"] != action:
                    raise OperationConflict(f"operation_id already used for {existing['action_type']}")
                return state.repair_result(doc, existing, replayed=True)

            if not checked_other_machines:
                other = self.col.find_one(
                    {"receipts.operation_id": operation_id, "_id": {"$ne": machine_id}}, {"_id": 1}
                )
                if other is not None:
                    raise OperationConflict(f"operation_id already used on {other['_id']}")
                checked_other_machines = True

            changes, receipt = state.plan_repair(doc, action, operation_id)
            update: dict[str, Any] = {
                "$push": {"receipts": {"$each": [receipt], "$slice": -state.MAX_RECEIPTS}},
                "$inc": {"version": 1},
            }
            if changes:
                update["$set"] = changes
            updated = self.col.find_one_and_update(
                {"_id": machine_id, "version": doc["version"], "receipts.operation_id": {"$ne": operation_id}},
                update,
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                return state.repair_result(updated, receipt, replayed=False)
            # Lost the race (someone else changed the doc): re-read and retry / replay.
            time.sleep(random.uniform(0.005, 0.02) * min(attempt + 1, 10))
        raise Contended(f"could not apply {action} on {machine_id} after {MAX_ATTEMPTS} attempts")

    def reset(self) -> list[dict[str, Any]]:
        """All machines healthy, counters and receipts cleared. version is bumped, never zeroed,
        so any in-flight compare-and-set from before the reset fails and re-reads."""
        self.ensure_machines()
        self.col.update_many(
            {"_id": {"$in": list(state.MACHINE_IDS)}},
            {"$set": {**state.healthy_state(), "receipts": []}, "$inc": {"version": 1}},
        )
        return self.list_machines()
