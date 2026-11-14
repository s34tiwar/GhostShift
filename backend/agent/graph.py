"""Constrained recovery proposal contract and initial deterministic planner."""

from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .procedures import MachineId, RecoveryTool
from .retrieval import retrieve_with_fallback


class RecoveryProposal(BaseModel):
    """The only information Shivani's planner passes to Rhea's executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: RecoveryTool
    machine_id: MachineId
    procedure_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)


async def propose_recovery(machine_health: Mapping[str, object]) -> RecoveryProposal:
    """Return a validated fallback proposal from current machine health.

    Atlas retrieval and LLM selection will replace the selection step later.
    This function never executes a repair or calls a state-changing endpoint.
    """

    evidence = retrieve_with_fallback(machine_health)
    procedure = evidence.procedure
    return RecoveryProposal(
        tool=procedure.allowed_tool,
        machine_id=machine_health.get("machine_id"),
        procedure_id=procedure.procedure_id,
        rationale=(
            "Deterministic fallback selected the active procedure matching "
            f"live fault {procedure.fault_type}."
        ),
    )
