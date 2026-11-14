"""Tests for Shivani's constrained proposal and retrieval foundation."""

import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from backend.agent.graph import (
    GEMINI_RESPONSE_SCHEMA,
    RecoveryProposal,
    build_proposal_graph,
    plan_recovery,
    propose_recovery,
    propose_recovery_with_trace,
)
from backend.agent.procedures import (
    NoMatchingProcedureError,
    SEED_PROCEDURES,
    get_fallback_procedure,
)
from backend.agent.retrieval import (
    EMBEDDING_DIMENSIONS,
    create_embedding,
    format_query_text,
    retrieve_from_atlas,
    retrieve_with_fallback,
)


SYNC_FAILURE = {
    "machine_id": "machine_b",
    "fault_type": "SYNC_PROCESS_DOWN",
    "sync_running": False,
    "network_connected": True,
    "backlog": 42,
    "message": "Edge sync process stopped",
}

NETWORK_FAILURE = {
    "machine_id": "machine_c",
    "fault_type": "NETWORK_DISCONNECTED",
    "sync_running": False,
    "network_connected": False,
    "backlog": 7,
    "message": "Network link unavailable",
}


class FakeGeminiModels:
    def __init__(
        self,
        dimensions: int = EMBEDDING_DIMENSIONS,
        proposal=None,
    ) -> None:
        self.dimensions = dimensions
        self.proposal = proposal
        self.embedding_calls = []
        self.generation_calls = []

    async def embed_content(self, **kwargs):
        self.embedding_calls.append(kwargs)
        item = SimpleNamespace(values=[0.01] * self.dimensions)
        return SimpleNamespace(embeddings=[item])

    async def generate_content(self, **kwargs):
        self.generation_calls.append(kwargs)
        return SimpleNamespace(parsed=self.proposal, text=None)


class FakeGemini:
    def __init__(
        self,
        dimensions: int = EMBEDDING_DIMENSIONS,
        proposal=None,
    ) -> None:
        self.models = FakeGeminiModels(dimensions, proposal)


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return self.documents[:length]


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents
        self.pipeline = None

    async def aggregate(self, pipeline):
        self.pipeline = pipeline
        return FakeCursor(self.documents)


class ProcedureTests(unittest.TestCase):
    def test_exactly_two_seed_procedures_cover_the_demo_faults(self) -> None:
        self.assertEqual(len(SEED_PROCEDURES), 2)
        self.assertEqual(
            {procedure.fault_type for procedure in SEED_PROCEDURES},
            {"SYNC_PROCESS_DOWN", "NETWORK_DISCONNECTED"},
        )

    def test_fallback_selects_the_sync_restart_procedure(self) -> None:
        procedure = get_fallback_procedure(SYNC_FAILURE)
        self.assertEqual(procedure.procedure_id, "proc_sync_restart")
        self.assertEqual(procedure.allowed_tool, "restart_sync")

    def test_fallback_refuses_an_unknown_fault(self) -> None:
        health = dict(SYNC_FAILURE, fault_type="UNKNOWN")
        with self.assertRaises(NoMatchingProcedureError):
            get_fallback_procedure(health)


class RetrievalTests(unittest.TestCase):
    def test_query_text_uses_the_locked_stable_format(self) -> None:
        self.assertEqual(
            format_query_text(SYNC_FAILURE),
            "fault=SYNC_PROCESS_DOWN; sync_running=false; "
            "network_connected=true; backlog=42; "
            "symptom=Edge sync process stopped",
        )

    def test_fallback_is_labelled_and_has_no_vector_score(self) -> None:
        result = retrieve_with_fallback(NETWORK_FAILURE)
        self.assertEqual(result.retrieval_mode, "deterministic_fallback")
        self.assertIsNone(result.score)


class AtlasRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_request_is_locked_to_1536_dimensions(self) -> None:
        client = FakeGemini()
        embedding = await create_embedding("factory recovery", client)
        self.assertEqual(len(embedding), EMBEDDING_DIMENSIONS)
        self.assertEqual(
            client.models.embedding_calls[0]["config"]["output_dimensionality"],
            EMBEDDING_DIMENSIONS,
        )

    async def test_embedding_rejects_an_unexpected_dimension(self) -> None:
        with self.assertRaises(ValueError):
            await create_embedding("factory recovery", FakeGemini(3))

    async def test_atlas_result_is_filtered_and_labelled(self) -> None:
        document = SEED_PROCEDURES[0].model_dump(exclude={"embedding"})
        document["score"] = 0.92
        collection = FakeCollection([document])

        result = await retrieve_from_atlas(
            SYNC_FAILURE, collection, FakeGemini()
        )

        self.assertEqual(result.retrieval_mode, "atlas_vector")
        self.assertEqual(result.procedure.procedure_id, "proc_sync_restart")
        self.assertEqual(result.score, 0.92)
        vector_search = collection.pipeline[0]["$vectorSearch"]
        self.assertEqual(
            vector_search["filter"],
            {"active": True, "fault_type": "SYNC_PROCESS_DOWN"},
        )


class ProposalTests(unittest.IsolatedAsyncioTestCase):
    def test_gemini_schema_contains_only_the_four_contract_fields(self) -> None:
        self.assertEqual(
            set(GEMINI_RESPONSE_SCHEMA["properties"]),
            {"tool", "machine_id", "procedure_id", "rationale"},
        )
        self.assertNotIn("additionalProperties", GEMINI_RESPONSE_SCHEMA)

    def test_langgraph_stops_at_proposal_without_execution_nodes(self) -> None:
        graph = build_proposal_graph(FakeCollection([]), FakeGemini())
        node_names = set(graph.get_graph().nodes)
        self.assertTrue({"inspect", "retrieve", "propose"} <= node_names)
        self.assertFalse({"execute", "verify"} & node_names)

    async def test_planner_proposes_restart_for_sync_failure(self) -> None:
        proposal = (await plan_recovery(SYNC_FAILURE)).proposal
        self.assertEqual(proposal.tool, "restart_sync")
        self.assertEqual(proposal.machine_id, "machine_b")
        self.assertEqual(proposal.procedure_id, "proc_sync_restart")

    async def test_planner_proposes_reconnect_for_network_failure(self) -> None:
        proposal = (await plan_recovery(NETWORK_FAILURE)).proposal
        self.assertEqual(proposal.tool, "reconnect_network")
        self.assertEqual(proposal.machine_id, "machine_c")

    def test_proposal_rejects_a_tool_outside_the_allowlist(self) -> None:
        with self.assertRaises(ValidationError):
            RecoveryProposal(
                tool="delete_data",
                machine_id="machine_b",
                procedure_id="bad_procedure",
                rationale="This must never be accepted.",
            )

    async def test_gemini_proposal_uses_atlas_evidence(self) -> None:
        document = SEED_PROCEDURES[0].model_dump(exclude={"embedding"})
        document["score"] = 0.92
        proposal = RecoveryProposal(
            tool="restart_sync",
            machine_id="machine_b",
            procedure_id="proc_sync_restart",
            rationale="The sync process is stopped while the network is healthy.",
        )
        gemini = FakeGemini(proposal=proposal)

        result = await plan_recovery(
            SYNC_FAILURE,
            collection=FakeCollection([document]),
            gemini_client=gemini,
        )

        self.assertEqual(result.planner_mode, "gemini")
        self.assertEqual(result.retrieval.retrieval_mode, "atlas_vector")
        self.assertEqual(result.proposal, proposal)
        self.assertEqual(len(gemini.models.generation_calls), 1)

    async def test_mismatched_gemini_tool_uses_visible_fallback(self) -> None:
        document = SEED_PROCEDURES[0].model_dump(exclude={"embedding"})
        document["score"] = 0.92
        mismatched = RecoveryProposal(
            tool="reconnect_network",
            machine_id="machine_b",
            procedure_id="proc_sync_restart",
            rationale="This tool conflicts with the retrieved procedure.",
        )

        result = await plan_recovery(
            SYNC_FAILURE,
            collection=FakeCollection([document]),
            gemini_client=FakeGemini(proposal=mismatched),
        )

        self.assertEqual(result.planner_mode, "deterministic_fallback")
        self.assertEqual(result.proposal.tool, "restart_sync")
        self.assertEqual(result.fallback_reason, "ValueError")

    async def test_trace_exposes_ui_evidence_without_changing_proposal(self) -> None:
        document = SEED_PROCEDURES[0].model_dump(exclude={"embedding"})
        document["score"] = 0.92
        proposal = RecoveryProposal(
            tool="restart_sync",
            machine_id="machine_b",
            procedure_id="proc_sync_restart",
            rationale="The retrieved procedure matches the live sync fault.",
        )

        trace = await propose_recovery_with_trace(
            SYNC_FAILURE,
            collection=FakeCollection([document]),
            gemini_client=FakeGemini(proposal=proposal),
        )

        self.assertEqual(
            set(trace["proposal"]),
            {"tool", "machine_id", "procedure_id", "rationale"},
        )
        self.assertEqual(trace["retrieval_mode"], "atlas_vector")
        self.assertEqual(trace["score"], 0.92)

    async def test_trace_labels_fallback_exactly_as_rhea_requested(self) -> None:
        trace = await propose_recovery_with_trace(
            SYNC_FAILURE,
            collection=FakeCollection([]),
            gemini_client=FakeGemini(),
        )

        self.assertEqual(trace["retrieval_mode"], "fallback_deterministic")
        self.assertIsNone(trace["score"])
        self.assertEqual(trace["proposal"]["tool"], "restart_sync")


if __name__ == "__main__":
    unittest.main()
