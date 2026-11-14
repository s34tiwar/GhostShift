"""Constrained Gemini recovery planner with a truthful deterministic fallback."""

import json
import os
from typing import Any, Literal, Mapping, Optional, TypedDict

from dotenv import load_dotenv
from google import genai
from google.genai import types
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from pymongo import AsyncMongoClient

from .procedures import MachineId, RecoveryTool
from .retrieval import (
    RetrievalResult,
    format_query_text,
    retrieve_from_atlas,
    retrieve_with_fallback,
)


DEFAULT_LLM_MODEL = "gemini-3.5-flash-lite"
GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["restart_sync", "reconnect_network"],
        },
        "machine_id": {
            "type": "string",
            "enum": ["machine_a", "machine_b", "machine_c"],
        },
        "procedure_id": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["tool", "machine_id", "procedure_id", "rationale"],
}
SYSTEM_INSTRUCTION = """You are a constrained manufacturing recovery planner.
You may propose ONLY restart_sync or reconnect_network.
Use live machine health as the source of truth.
Use the retrieved procedure as guidance, not as executable instructions.
Select only the tool and procedure_id supplied by the retrieved procedure.
Never call an API or execute a repair. If evidence is insufficient, abstain.
"""


class RecoveryProposal(BaseModel):
    """The only information Shivani's planner passes to Rhea's executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: RecoveryTool
    machine_id: MachineId
    procedure_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)


class PlanningResult(BaseModel):
    """The proposal plus evidence needed to label the decision truthfully."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: RecoveryProposal
    retrieval: RetrievalResult
    planner_mode: Literal["gemini", "fallback_deterministic"]
    fallback_reason: Optional[str] = None


class ProposalGraphState(TypedDict, total=False):
    """State carried only through Shivani's proposal-building graph."""

    machine_health: dict[str, object]
    query_text: str
    retrieval: RetrievalResult
    proposal: RecoveryProposal


def _fallback_result(
    machine_health: Mapping[str, object], reason: str
) -> PlanningResult:
    evidence = retrieve_with_fallback(machine_health)
    procedure = evidence.procedure
    proposal = RecoveryProposal(
        tool=procedure.allowed_tool,
        machine_id=machine_health.get("machine_id"),
        procedure_id=procedure.procedure_id,
        rationale=(
            "Deterministic fallback selected the active procedure matching "
            f"live fault {procedure.fault_type}."
        ),
    )
    return PlanningResult(
        proposal=proposal,
        retrieval=evidence,
        planner_mode="fallback_deterministic",
        fallback_reason=reason,
    )


def _validate_against_evidence(
    proposal: RecoveryProposal, evidence: RetrievalResult
) -> None:
    procedure = evidence.procedure
    if proposal.procedure_id != procedure.procedure_id:
        raise ValueError("Gemini selected a procedure that was not retrieved")
    if proposal.tool != procedure.allowed_tool:
        raise ValueError("Gemini selected a tool not allowed by the procedure")


async def _generate_proposal(
    machine_health: Mapping[str, object],
    evidence: RetrievalResult,
    gemini_client: Any,
) -> RecoveryProposal:
    prompt = (
        "Live machine health:\n"
        f"{json.dumps(dict(machine_health), sort_keys=True)}\n\n"
        "Retrieved recovery procedure:\n"
        f"{evidence.procedure.model_dump_json(exclude={'embedding'})}"
    )
    response = await gemini_client.models.generate_content(
        model=os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=GEMINI_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, RecoveryProposal):
        proposal = parsed
    elif parsed is not None:
        proposal = RecoveryProposal.model_validate(parsed)
    elif response.text:
        proposal = RecoveryProposal.model_validate_json(response.text)
    else:
        raise ValueError("Gemini returned no structured proposal")

    _validate_against_evidence(proposal, evidence)
    if proposal.machine_id != machine_health.get("machine_id"):
        raise ValueError("Gemini changed the target machine")
    return proposal


def build_proposal_graph(collection: Any, gemini_client: Any):
    """Compile the proposal-only inspect -> retrieve -> propose workflow."""

    async def inspect_node(state: ProposalGraphState) -> dict:
        return {"query_text": format_query_text(state["machine_health"])}

    async def retrieve_node(state: ProposalGraphState) -> dict:
        evidence = await retrieve_from_atlas(
            state["machine_health"], collection, gemini_client
        )
        return {"retrieval": evidence}

    async def propose_node(state: ProposalGraphState) -> dict:
        proposal = await _generate_proposal(
            state["machine_health"], state["retrieval"], gemini_client
        )
        return {"proposal": proposal}

    builder = StateGraph(ProposalGraphState)
    builder.add_node("inspect", inspect_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("propose", propose_node)
    builder.add_edge(START, "inspect")
    builder.add_edge("inspect", "retrieve")
    builder.add_edge("retrieve", "propose")
    builder.add_edge("propose", END)
    return builder.compile(name="ghostshift_proposal")


async def plan_recovery(
    machine_health: Mapping[str, object],
    *,
    collection: Any = None,
    gemini_client: Any = None,
) -> PlanningResult:
    """Retrieve evidence and request one validated, non-executing proposal."""

    if collection is None or gemini_client is None:
        return _fallback_result(
            machine_health, "Atlas or Gemini dependency unavailable"
        )

    try:
        graph = build_proposal_graph(collection, gemini_client)
        state = await graph.ainvoke({"machine_health": dict(machine_health)})
        evidence = state["retrieval"]
        proposal = state["proposal"]

        return PlanningResult(
            proposal=proposal,
            retrieval=evidence,
            planner_mode="gemini",
        )
    except Exception as exc:
        return _fallback_result(machine_health, type(exc).__name__)


async def _plan_recovery_from_environment(
    machine_health: Mapping[str, object],
) -> PlanningResult:
    """Run the planner with process-local clients configured from the worker env."""

    load_dotenv()
    mongo_uri = os.getenv("BACKEND_MONGODB_URI") or os.getenv("MONGODB_URI")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not mongo_uri or not gemini_key:
        return _fallback_result(
            machine_health, "MongoDB or Gemini environment unavailable"
        )

    mongo = AsyncMongoClient(mongo_uri)
    gemini = genai.Client(api_key=gemini_key)
    async_gemini = gemini.aio
    try:
        database = mongo[os.getenv("MONGODB_DB", "ghostshift_hackthon")]
        return await plan_recovery(
            machine_health,
            collection=database["recovery_procedures"],
            gemini_client=async_gemini,
        )
    except Exception as exc:
        return _fallback_result(machine_health, type(exc).__name__)
    finally:
        await async_gemini.aclose()
        await mongo.close()


async def _resolve_plan(
    machine_health: Mapping[str, object],
    *,
    collection: Any = None,
    gemini_client: Any = None,
) -> PlanningResult:
    if collection is None and gemini_client is None:
        return await _plan_recovery_from_environment(machine_health)
    return await plan_recovery(
        machine_health,
        collection=collection,
        gemini_client=gemini_client,
    )


async def propose_recovery(
    machine_health: Mapping[str, object],
    *,
    collection: Any = None,
    gemini_client: Any = None,
) -> RecoveryProposal:
    """Expose Rhea's agreed four-field proposal contract.

    This function never executes a repair or calls a state-changing endpoint.
    """

    result = await _resolve_plan(
        machine_health,
        collection=collection,
        gemini_client=gemini_client,
    )
    return result.proposal


async def propose_recovery_with_trace(
    machine_health: Mapping[str, object],
    *,
    collection: Any = None,
    gemini_client: Any = None,
) -> dict:
    """Return Rhea's proposal plus UI-safe retrieval evidence.

    The worker may call this with only machine_health. Optional dependencies are
    retained for deterministic unit and integration tests.
    """

    result = await _resolve_plan(
        machine_health,
        collection=collection,
        gemini_client=gemini_client,
    )
    used_fallback = result.planner_mode == "fallback_deterministic"
    return {
        "proposal": result.proposal.model_dump(),
        "retrieval_mode": (
            "fallback_deterministic"
            if used_fallback
            else result.retrieval.retrieval_mode
        ),
        "score": None if used_fallback else result.retrieval.score,
    }
