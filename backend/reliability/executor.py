"""Guarded execution, verification, and reconciliation. Owner: Rhea.

Effectively-once via idempotent operations: durable intent, a stable
operation_id, the factory's operation receipt, read-back verification, and
safe reconciliation. An HTTP 200 from the factory is never proof on its own.
"""
from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx

from reliability import ledger, policies

PROPOSAL_FIELDS = ("tool", "machine_id", "procedure_id", "rationale")
MAX_ATTEMPTS = 2  # the original call plus one same-key retry
RETRY_503_MAX = 3  # extra HTTP attempts on a 503 (the factory says "just retry")
RETRY_503_BACKOFF = 0.3  # seconds between those attempts

# Unique per process boot, so a restarted worker can reclaim its own dead claim.
_DEFAULT_OWNER = f"worker_{os.getpid()}_{uuid4().hex[:8]}"


# --- factory client ----------------------------------------------------------


class FactoryClient:
    """Thin HTTP client for the factory service. Never logs the base URL."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        base_url = base_url or os.environ.get("FACTORY_BASE_URL")
        if not base_url:
            raise RuntimeError("FACTORY_BASE_URL is not set")
        # Optional shared secret. The factory requires X-Factory-Token on every POST
        # (GETs are open). Kept private and never logged or put in an error message.
        self._token = token or os.environ.get("FACTORY_TOKEN") or None
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(5.0, connect=2.0)
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """One factory call. Retries HTTP 503 only ("lost races", safe to repeat).

        POST bodies carry the same operation_id on every attempt, so a retry can
        never act twice. Timeouts, 409 and other 4xx/5xx are NOT retried here: a
        timed-out POST goes straight to read-back verification.
        """
        for attempt in range(1 + RETRY_503_MAX):
            response = await self._http.request(method, path, **kwargs)
            if response.status_code != 503 or attempt == RETRY_503_MAX:
                break
            await asyncio.sleep(RETRY_503_BACKOFF)
        response.raise_for_status()
        return response.json()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, body: dict) -> dict:
        headers = {"X-Factory-Token": self._token} if self._token else None
        return await self._request("POST", path, json=body, headers=headers)

    async def health(self, machine_id: str) -> dict:
        return await self._get(f"/machines/{machine_id}/health")

    async def restart_sync(self, machine_id: str, operation_id: str) -> dict:
        return await self._post(
            f"/machines/{machine_id}/restart-sync", {"operation_id": operation_id}
        )

    async def reconnect(self, machine_id: str, operation_id: str) -> dict:
        return await self._post(
            f"/machines/{machine_id}/reconnect", {"operation_id": operation_id}
        )

    async def verify(self, machine_id: str, expected_action: str, operation_id: str) -> dict:
        return await self._get(
            f"/machines/{machine_id}/verify",
            {"expected_action": expected_action, "operation_id": operation_id},
        )

    async def aclose(self) -> None:
        await self._http.aclose()


# Tool name -> FactoryClient method name. A tool that is not here can never be
# sent to the factory, whatever the policy says.
_TOOL_METHODS = {"restart_sync": "restart_sync", "reconnect_network": "reconnect"}

_factory: FactoryClient | None = None


def get_factory() -> FactoryClient:
    """Lazy per-process default client."""
    global _factory
    if _factory is None:
        _factory = FactoryClient()
    return _factory


# --- crash injection ---------------------------------------------------------


class CrashNotAllowedError(RuntimeError):
    """_crash() was called outside a process that opted in to dying."""


# Only worker.main() sets this, at boot. Anywhere else (API, scripts, an unpatched
# test) the demo crash raises instead of killing the interpreter.
REAL_CRASH = False


def _crash() -> None:
    """Kill this worker process only. Tests patch this."""
    if not REAL_CRASH:
        raise CrashNotAllowedError("os._exit(42) is only allowed in the worker process")
    os._exit(42)


async def maybe_crash_after_external_effect(run_id: str, action_id: str | None = None) -> None:
    """DEMO CRASH POINT: the effect may exist but the ledger is not verified.

    crash_fired is persisted in Atlas BEFORE the process dies, so the same run
    can never crash twice.
    """
    if await ledger.fire_crash_once(run_id):
        await _audit(run_id, action_id, "executor", "CRASH_INJECTED",
                     "demo crash after external effect, before verification")
        _crash()


# --- internals ---------------------------------------------------------------


async def _audit(run_id, action_id, actor, event_type, reason=None, detail=None) -> None:
    await ledger.append_audit(run_id, action_id, actor, event_type, None, reason, detail, None)


def _proposal_error(proposal) -> str | None:
    if not isinstance(proposal, dict):
        return "malformed proposal: not an object"
    for field in PROPOSAL_FIELDS:
        value = proposal.get(field)
        if not isinstance(value, str) or not value:
            return f"malformed proposal: missing or invalid {field}"
    return None


async def _verify(factory, action: dict) -> tuple[str, dict]:
    """Read the effect back from the factory: 'present', 'absent' or 'ambiguous'."""
    try:
        actual = await factory.verify(
            action["target"], action["action_type"], action["operation_id"]
        )
    except Exception as exc:  # timeout, 5xx, bad JSON: we do not know
        return "ambiguous", {"effect_present": False, "error": type(exc).__name__}
    if not isinstance(actual, dict) or not isinstance(actual.get("effect_present"), bool):
        return "ambiguous", {"effect_present": False, "error": "malformed verify response"}
    return ("present" if actual["effect_present"] else "absent"), actual


async def _mark_verified(run_id, action, actual, owner) -> dict:
    aid = action["action_id"]
    updated = await ledger.mark_effect_verified(aid, actual)
    await _audit(run_id, aid, owner, "EFFECT_VERIFIED", "effect read back from factory",
                 {"operation_id": action["operation_id"]})
    return updated


async def _mark_review(run_id, action, actual, owner, reason) -> dict:
    aid = action["action_id"]
    updated = await ledger.mark_requires_review(aid, actual)
    await _audit(run_id, aid, owner, "REQUIRES_REVIEW", reason,
                 {"operation_id": action["operation_id"]})
    return updated


async def _execute_claimed(run_id, action, factory, owner) -> dict:
    """Call the factory for an action THIS owner has just claimed, then verify."""
    aid = action["action_id"]
    op = action["operation_id"]
    await ledger.set_run_status(run_id, "RUNNING", current_action_id=aid)
    await _audit(run_id, aid, owner, "EXECUTION_STARTED", None, {"operation_id": op})

    method = getattr(factory, _TOOL_METHODS[action["action_type"]])
    receipt = None
    try:
        receipt = await method(action["target"], op)
    except Exception as exc:
        # Unknown outcome. Never retry on a timeout: fall through to read-back.
        await _audit(run_id, aid, owner, "TOOL_CALL_ERROR", type(exc).__name__,
                     {"operation_id": op})

    if receipt is not None:
        await ledger.save_receipt(aid, receipt)
        await maybe_crash_after_external_effect(run_id, aid)

    kind, actual = await _verify(factory, action)
    if kind == "present":
        return await _mark_verified(run_id, action, actual, owner)
    reason = "verification ambiguous" if kind == "ambiguous" else "effect absent after tool call"
    return await _mark_review(run_id, action, actual, owner, reason)


async def _authorize_and_execute(run_id, action, factory, owner) -> dict:
    """PLANNED -> policy check -> AUTHORIZED -> atomic claim -> execute."""
    aid = action["action_id"]
    # A finished or aborted run never gets a new execution (RunTerminalError).
    await ledger.ensure_run_active(run_id)
    decision = await policies.authorize(run_id, aid, action["proposal"])
    if decision.allowed and action["action_type"] not in _TOOL_METHODS:
        decision.allowed = False
        decision.reason = f"no executor for tool: {action['action_type']}"
    if not decision.allowed:
        await ledger.mark_denied(aid, decision.reason, decision.policy_version)
        return await ledger.get_action(aid)

    await ledger.mark_authorized(aid, decision)
    if not await ledger.claim_action_atomically(aid, owner):
        # Someone else owns it. Read-only reconcile: never act without a claim.
        return await reconcile_action(aid, factory, owner, retry=False)
    return await _execute_claimed(run_id, action, factory, owner)


async def _sync_run(run_id: str, action: dict) -> None:
    """Reflect a single action's outcome on its run."""
    status = {
        "EFFECT_VERIFIED": "COMPLETED",
        "DENIED": "DENIED",
        "REQUIRES_REVIEW": "NEEDS_RECONCILIATION",
    }.get(action["status"])
    if status == "COMPLETED":
        await ledger.set_run_status(run_id, status, current_action_id=action["action_id"],
                                    last_verified_step=action["action_id"])
    elif status is not None:
        await ledger.set_run_status(run_id, status, current_action_id=action["action_id"])


# --- public API --------------------------------------------------------------


async def execute_guarded_action(run_id: str, proposal: dict, factory=None, owner=None) -> dict:
    """Validate, authorize, persist intent, execute, verify. Safe to call again."""
    factory = factory or get_factory()
    owner = owner or _DEFAULT_OWNER

    epoch = await ledger.get_fault_epoch(run_id)  # ValueError if the run is missing

    error = _proposal_error(proposal)
    if error is not None:
        # Not enough to build an operation_id: audit the denial, touch nothing else.
        await ledger.append_audit(run_id, None, policies.ACTOR, "POLICY_CHECK",
                                  "DENIED", error, {}, None)
        await ledger.set_run_status(run_id, "DENIED")
        return {"status": "DENIED", "reason": error}

    if epoch is None:
        machine_id = proposal["machine_id"]
        live = 0  # invalid machine: no health to read, but the denial is still recorded
        if machine_id in policies.KNOWN_MACHINES:
            live = int((await factory.health(machine_id))["fault_epoch"])
        epoch = await ledger.pin_fault_epoch(run_id, live)

    action = await ledger.create_action(run_id, proposal, epoch)  # stable operation_id
    status = action["status"]
    if status in ("EFFECT_VERIFIED", "DENIED", "REQUIRES_REVIEW"):
        return action
    if status == "PLANNED":
        result = await _authorize_and_execute(run_id, action, factory, owner)
    else:  # AUTHORIZED / EXECUTING left by an earlier attempt
        result = await reconcile_action(action["action_id"], factory, owner)
    await _sync_run(run_id, result)
    return result


async def reconcile_action(action_id: str, factory=None, owner=None, retry: bool = True) -> dict:
    """Settle an unfinished action from what the factory actually shows.

    retry=False makes this read-only: it may mark an effect verified but never
    calls a factory tool.
    """
    factory = factory or get_factory()
    owner = owner or _DEFAULT_OWNER

    action = await ledger.get_action(action_id)
    if action is None:
        raise ValueError(f"action {action_id} does not exist")
    run_id = action["run_id"]
    status = action["status"]
    if status in ("EFFECT_VERIFIED", "DENIED"):
        return action
    # An unfinished action on a finished or aborted run is never verified, retried
    # or re-executed by anything (RunTerminalError, before any factory call).
    await ledger.ensure_run_active(run_id)
    if status == "PLANNED":  # crashed before authorization: nothing can have run
        return await _authorize_and_execute(run_id, action, factory, owner)

    kind, actual = await _verify(factory, action)
    if kind == "present":
        # Already applied: same operation_id, no new factory mutation.
        return await _mark_verified(run_id, action, actual, owner)
    if status == "REQUIRES_REVIEW" or not retry:
        return action
    if kind == "ambiguous":
        return await _mark_review(run_id, action, actual, owner, "verification ambiguous")

    # Verified absent from here on.
    if actual.get("receipt_exists"):
        return await _mark_review(run_id, action, actual, owner,
                                  "factory holds a receipt but the effect is absent")
    if action.get("attempt_count", 0) >= MAX_ATTEMPTS:
        return await _mark_review(run_id, action, actual, owner, "retry budget exhausted")

    decision = await policies.authorize(run_id, action_id, action["proposal"])
    if not decision.allowed:
        return await _mark_review(run_id, action, actual, owner,
                                  f"policy no longer allows retry: {decision.reason}")
    await ledger.mark_authorized(action_id, decision)
    if not await ledger.reclaim_stale_action(action_id, owner):
        return await ledger.get_action(action_id)  # not ours: do not act

    await _audit(run_id, action_id, owner, "RECONCILE_RETRY",
                 "effect verified absent; retrying once with the same operation_id",
                 {"operation_id": action["operation_id"]})
    return await _execute_claimed(run_id, action, factory, owner)


async def reconcile_run(run_id: str, factory=None, owner=None) -> dict:
    """Settle every unfinished action of a run, then settle the run itself."""
    factory = factory or get_factory()
    owner = owner or _DEFAULT_OWNER

    for action in await ledger.get_machine_ledger(run_id):
        if action["status"] not in ("EFFECT_VERIFIED", "DENIED"):
            await reconcile_action(action["action_id"], factory, owner)

    actions = await ledger.get_machine_ledger(run_id)
    statuses = {a["status"] for a in actions}
    if "REQUIRES_REVIEW" in statuses:
        # Re-verified above and still unresolved: needs a human. Terminal.
        await ledger.set_run_status(run_id, "FAILED")
    elif "DENIED" in statuses:
        await ledger.set_run_status(run_id, "DENIED")
    elif actions and statuses == {"EFFECT_VERIFIED"}:
        await ledger.set_run_status(run_id, "COMPLETED",
                                    last_verified_step=actions[-1]["action_id"])
    # No actions, or one still owned by a live worker: leave the status alone.

    run = await ledger.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} does not exist")
    return {"run_id": run_id, "status": run["status"], "actions": actions}
