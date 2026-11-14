"""Gemini embeddings, Atlas Vector Search, and explicit fallback retrieval."""

import os
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from .procedures import RecoveryProcedure, get_fallback_procedure


EMBEDDING_DIMENSIONS = 1536
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"
DEFAULT_VECTOR_INDEX = "recovery_vector_index"


class RetrievalResult(BaseModel):
    """Procedure evidence returned to the planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    procedure: RecoveryProcedure
    retrieval_mode: Literal["atlas_vector", "fallback_deterministic"]
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
        retrieval_mode="fallback_deterministic",
    )


async def create_embedding(
    text: str,
    gemini_client: Any,
    *,
    model: Optional[str] = None,
) -> List[float]:
    """Create and validate one 1536-dimensional Gemini embedding."""

    if not text.strip():
        raise ValueError("Embedding input cannot be empty")

    response = await gemini_client.models.embed_content(
        model=model or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        contents=text,
        config={"output_dimensionality": EMBEDDING_DIMENSIONS},
    )
    if not response.embeddings or len(response.embeddings) != 1:
        raise ValueError("Expected exactly one embedding result")

    embedding = list(response.embeddings[0].values)
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Expected {EMBEDDING_DIMENSIONS} embedding dimensions, "
            f"received {len(embedding)}"
        )
    return embedding


async def retrieve_from_atlas(
    health: Mapping[str, object],
    collection: Any,
    gemini_client: Any,
    *,
    index_name: Optional[str] = None,
) -> RetrievalResult:
    """Retrieve and validate the best compatible procedure from Atlas."""

    query_embedding = await create_embedding(
        format_query_text(health), gemini_client
    )
    pipeline = [
        {
            "$vectorSearch": {
                "index": index_name
                or os.getenv("VECTOR_INDEX_NAME", DEFAULT_VECTOR_INDEX),
                "path": "embedding",
                "queryVector": query_embedding,
                "numCandidates": 20,
                "limit": 3,
                "filter": {
                    "active": True,
                    "fault_type": health.get("fault_type"),
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "procedure_id": 1,
                "title": 1,
                "description": 1,
                "fault_type": 1,
                "allowed_tool": 1,
                "machine_scope": 1,
                "active": 1,
                "version": 1,
                "source": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    cursor = await collection.aggregate(pipeline)
    candidates = await cursor.to_list(length=3)
    machine_id = health.get("machine_id")
    fault_type = health.get("fault_type")

    for candidate in candidates:
        score = candidate.pop("score", None)
        procedure = RecoveryProcedure.model_validate(candidate)
        if (
            procedure.active
            and procedure.fault_type == fault_type
            and machine_id in procedure.machine_scope
            and score is not None
        ):
            return RetrievalResult(
                procedure=procedure,
                retrieval_mode="atlas_vector",
                score=float(score),
            )

    raise LookupError("Atlas returned no active, compatible recovery procedure")
