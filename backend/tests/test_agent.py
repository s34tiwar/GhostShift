"""Tests for Shivani's constrained proposal and retrieval foundation."""

import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from backend.agent.graph import RecoveryProposal, propose_recovery
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


class FakeEmbeddings:
    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = SimpleNamespace(embedding=[0.01] * self.dimensions)
        return SimpleNamespace(data=[item])


class FakeOpenAI:
    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.embeddings = FakeEmbeddings(dimensions)


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
        client = FakeOpenAI()
        embedding = await create_embedding("factory recovery", client)
        self.assertEqual(len(embedding), EMBEDDING_DIMENSIONS)
        self.assertEqual(
            client.embeddings.calls[0]["dimensions"], EMBEDDING_DIMENSIONS
        )

    async def test_embedding_rejects_an_unexpected_dimension(self) -> None:
        with self.assertRaises(ValueError):
            await create_embedding("factory recovery", FakeOpenAI(3))

    async def test_atlas_result_is_filtered_and_labelled(self) -> None:
        document = SEED_PROCEDURES[0].model_dump(exclude={"embedding"})
        document["score"] = 0.92
        collection = FakeCollection([document])

        result = await retrieve_from_atlas(
            SYNC_FAILURE, collection, FakeOpenAI()
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
    async def test_planner_proposes_restart_for_sync_failure(self) -> None:
        proposal = await propose_recovery(SYNC_FAILURE)
        self.assertEqual(proposal.tool, "restart_sync")
        self.assertEqual(proposal.machine_id, "machine_b")
        self.assertEqual(proposal.procedure_id, "proc_sync_restart")

    async def test_planner_proposes_reconnect_for_network_failure(self) -> None:
        proposal = await propose_recovery(NETWORK_FAILURE)
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


if __name__ == "__main__":
    unittest.main()
