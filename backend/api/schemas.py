"""Shared request, response, and enum contracts. Freeze early as a team."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# All timestamps in these models are UTC ISO-8601 strings.

# --- Enums -------------------------------------------------------------------

MachineId = Literal["machine_a", "machine_b", "machine_c"]
FaultType = Literal["NONE", "SYNC_PROCESS_DOWN", "NETWORK_DISCONNECTED"]
ActionType = Literal["restart_sync", "reconnect_network", "shutdown_factory"]
RunStatus = Literal[
    "QUEUED",
    "CREATED",
    "DIAGNOSING",
    "ACTION_PENDING",
    "RUNNING",
    "NEEDS_RECONCILIATION",
    "COMPLETED",
    "FAILED",
    "DENIED",
]
ActionStatus = Literal[
    "PLANNED",
    "AUTHORIZED",
    "EXECUTING",
    "EFFECT_VERIFIED",
    "DENIED",
    "FAILED",
    "REQUIRES_REVIEW",
]

# --- Models ------------------------------------------------------------------


class Proposal(BaseModel):
    """Action proposal handed over by the agent."""

    tool: str
    machine_id: str
    procedure_id: str
    rationale: str


class MachineHealth(BaseModel):
    """Factory GET /machines/{id}/health response. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    machine_id: MachineId
    healthy: bool
    fault_type: FaultType
    sync_running: bool
    network_connected: bool
    backlog: int
    fault_epoch: int
    last_operation_id: str | None = None
    restart_count: int = 0
    reconnect_count: int = 0
    message: str | None = None


class RunView(BaseModel):
    """get_run(run_id) return shape."""

    run_id: str
    status: RunStatus
    machine_id: MachineId
    procedure_id: str | None = None
    retrieval_mode: str | None = None
    current_action_id: str | None = None
    restart_count: int = 0  # manual resumes
    recovery_count: int = 0  # worker takeovers
    worker_status: str | None = None


class AuditEvent(BaseModel):
    """One entry of list_audit(run_id)['events']."""

    at: str
    actor: str
    event_type: str
    reason: str | None = None
    action_id: str | None = None
    policy_version: int | None = None


class AuditResponse(BaseModel):
    """list_audit(run_id) return shape."""

    events: list[AuditEvent]


class RunCreateRequest(BaseModel):
    """POST /api/runs body."""

    machine_id: MachineId
    crash_armed: bool = False


class RunCreateResponse(BaseModel):
    """POST /api/runs 202 response."""

    run_id: str
    status: RunStatus


class CrashArmRequest(BaseModel):
    """POST /api/demo/crash-arm body."""

    run_id: str
    stage: Literal["after_effect_before_verify"]


class PolicyDecision(BaseModel):
    allowed: bool
    policy_version: int | None = None
    reason: str | None = None


class Verification(BaseModel):
    effect_present: bool
    checked_at: str | None = None


class LedgerEntry(BaseModel):
    """action_ledger document."""

    action_id: str
    run_id: str
    operation_id: str
    agent_id: str
    target: MachineId
    action_type: ActionType
    status: ActionStatus
    policy_decision: PolicyDecision | None = None
    attempt_count: int = 0
    external_receipt: dict | None = None
    verification: Verification | None = None
    started_at: str | None = None
    verified_at: str | None = None


# --- ID helpers --------------------------------------------------------------


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


def new_action_id() -> str:
    return f"act_{uuid4().hex}"


def make_operation_id(run_id: str, machine_id: str, tool: str, fault_epoch: int) -> str:
    return f"{run_id}:{machine_id}:{tool}:epoch_{fault_epoch}"
