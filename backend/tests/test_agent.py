"""Tests for Shivani's constrained proposal and retrieval foundation."""

import unittest

from pydantic import ValidationError

from backend.agent.graph import RecoveryProposal, propose_recovery
from backend.agent.procedures import (
    NoMatchingProcedureError,
    SEED_PROCEDURES,
    get_fallback_procedure,
)
from backend.agent.retrieval import format_query_text, retrieve_with_fallback


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
