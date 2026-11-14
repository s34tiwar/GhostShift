"""Recovery procedure definitions, seed data, and deterministic fallback."""

from typing import Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


MachineId = Literal["machine_a", "machine_b", "machine_c"]
FaultType = Literal["SYNC_PROCESS_DOWN", "NETWORK_DISCONNECTED"]
RecoveryTool = Literal["restart_sync", "reconnect_network"]


class RecoveryProcedure(BaseModel):
    """A reviewed runbook that can guide the constrained planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    procedure_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    fault_type: FaultType
    allowed_tool: RecoveryTool
    machine_scope: List[MachineId] = Field(min_length=1)
    active: bool = True
    version: int = Field(default=1, ge=1)
    embedding: Optional[List[float]] = None
    source: str = "hackathon-authored synthetic runbook"


class NoMatchingProcedureError(LookupError):
    """Raised when no active, scoped fallback procedure matches live health."""


SEED_PROCEDURES: Tuple[RecoveryProcedure, ...] = (
    RecoveryProcedure(
        procedure_id="proc_sync_restart",
        title="Restart a failed edge sync worker",
        description=(
            "Use when synchronization is stopped while the network remains "
            "connected. Restart only the synchronization process."
        ),
        fault_type="SYNC_PROCESS_DOWN",
        allowed_tool="restart_sync",
        machine_scope=["machine_a", "machine_b", "machine_c"],
    ),
    RecoveryProcedure(
        procedure_id="proc_network_reconnect",
        title="Reconnect an offline factory machine",
        description=(
            "Use when the machine has lost network connectivity. Restore the "
            "network connection before expecting synchronization to recover."
        ),
        fault_type="NETWORK_DISCONNECTED",
        allowed_tool="reconnect_network",
        machine_scope=["machine_a", "machine_b", "machine_c"],
    ),
)

_PROCEDURES_BY_FAULT: Dict[str, RecoveryProcedure] = {
    procedure.fault_type: procedure for procedure in SEED_PROCEDURES
}


def get_fallback_procedure(health: Mapping[str, object]) -> RecoveryProcedure:
    """Choose a reviewed procedure by exact fault match and machine scope."""

    fault_type = health.get("fault_type")
    machine_id = health.get("machine_id")
    procedure = _PROCEDURES_BY_FAULT.get(str(fault_type))

    if (
        procedure is None
        or not procedure.active
        or machine_id not in procedure.machine_scope
    ):
        raise NoMatchingProcedureError(
            f"No active fallback procedure for fault={fault_type!r}, "
            f"machine={machine_id!r}"
        )

    return procedure


def format_procedure_text(procedure: RecoveryProcedure) -> str:
    """Build stable text for a procedure's stored embedding."""

    return (
        f"fault={procedure.fault_type}; "
        f"title={procedure.title}; "
        f"description={procedure.description}; "
        f"allowed_tool={procedure.allowed_tool}"
    )
