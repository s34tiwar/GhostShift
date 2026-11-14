"""Deny-by-default action authorization. Owner: Rhea."""
from __future__ import annotations

from typing import get_args

from pymongo import DESCENDING

from api.schemas import ActionType, MachineId, PolicyDecision
from reliability import ledger, storage

# Fixed allowlists: anything outside these is rejected before policy lookup.
KNOWN_TOOLS = frozenset(get_args(ActionType))
KNOWN_MACHINES = frozenset(get_args(MachineId))

ACTOR = "policy_engine"


async def seed_default_policy() -> None:
    """Idempotent. Never overwrites an existing v1 policy (e.g. one disabled by hand)."""
    await storage.policies().update_one(
        {"agent_id": storage.AGENT_ID, "policy_version": 1},
        {
            "$setOnInsert": {
                "enabled": True,
                "machine_scope": ["machine_a", "machine_b", "machine_c"],
                "allowed_actions": ["restart_sync", "reconnect_network"],
                "denied_actions": ["shutdown_factory", "delete_data"],
            }
        },
        upsert=True,
    )


async def _latest_policy() -> dict | None:
    return await storage.policies().find_one(
        {"agent_id": storage.AGENT_ID, "enabled": True},
        sort=[("policy_version", DESCENDING)],
    )


async def _decide(proposal) -> tuple[bool, int | None, str]:
    """Return (allowed, policy_version, reason). Deny by default."""
    if not isinstance(proposal, dict):
        return False, None, "malformed proposal"
    tool = proposal.get("tool")
    machine_id = proposal.get("machine_id")

    if tool not in KNOWN_TOOLS:
        return False, None, f"tool not in allowlist: {tool!r}"
    if machine_id not in KNOWN_MACHINES:
        return False, None, f"unknown machine: {machine_id!r}"

    policy = await _latest_policy()
    if policy is None:
        return False, None, "no enabled policy for agent"
    version = policy["policy_version"]

    if tool in policy.get("denied_actions", []):
        return False, version, f"action explicitly denied by policy: {tool}"
    if tool not in policy.get("allowed_actions", []):
        return False, version, f"action not allowed by policy: {tool}"
    if machine_id not in policy.get("machine_scope", []):
        return False, version, f"machine out of policy scope: {machine_id}"
    return True, version, f"{tool} allowed on {machine_id}"


async def authorize(run_id: str, action_id: str, proposal: dict) -> PolicyDecision:
    allowed, version, reason = await _decide(proposal)
    detail = (
        {k: proposal.get(k) for k in ("tool", "machine_id", "procedure_id")}
        if isinstance(proposal, dict)
        else {}
    )
    # Always record the decision, allowed or denied.
    await ledger.append_audit(
        run_id,
        action_id,
        actor=ACTOR,
        event_type="POLICY_CHECK",
        decision="ALLOWED" if allowed else "DENIED",
        reason=reason,
        detail=detail,
        policy_version=version,
    )
    return PolicyDecision(allowed=allowed, policy_version=version, reason=reason)
