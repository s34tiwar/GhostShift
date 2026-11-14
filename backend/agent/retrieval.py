"""Stable retrieval input and explicitly labelled fallback retrieval."""

from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from .procedures import RecoveryProcedure, get_fallback_procedure


class RetrievalResult(BaseModel):
    """Procedure evidence returned to the planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    procedure: RecoveryProcedure
    retrieval_mode: Literal["atlas_vector", "deterministic_fallback"]
    score: Optional[float] = Field(default=None, ge=0.0)


def _require_bool(health: Mapping[str, object], key: str) -> bool:
    value = health.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"health[{key!r}] must be a boolean")
    return value


def format_query_text(health: Mapping[str, object]) -> str:
    """Build the locked, stable text embedded for Atlas Vector Search."""

    required = ("fault_type", "sync_running", "network_connected", "backlog")
    missing = [key for key in required if key not in health]
    if missing:
        raise ValueError(f"Missing health fields: {', '.join(missing)}")

    sync_running = _require_bool(health, "sync_running")
    network_connected = _require_bool(health, "network_connected")
    backlog = health["backlog"]
    if not isinstance(backlog, int) or isinstance(backlog, bool) or backlog < 0:
        raise ValueError("health['backlog'] must be a non-negative integer")

    symptom = str(health.get("message", "unknown"))[:120]
    return (
        f"fault={health['fault_type']}; "
        f"sync_running={str(sync_running).lower()}; "
        f"network_connected={str(network_connected).lower()}; "
        f"backlog={backlog}; "
        f"symptom={symptom}"
    )


def retrieve_with_fallback(health: Mapping[str, object]) -> RetrievalResult:
    """Return exact-match evidence without claiming Atlas Vector Search ran."""

    return RetrievalResult(
        procedure=get_fallback_procedure(health),
        retrieval_mode="deterministic_fallback",
    )
