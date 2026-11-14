"""Worker lease, pickup, crash-recovery and planner tests. Owner: Rhea."""
import asyncio

import pytest

from reliability import executor, ledger, storage, worker
from tests.fakes import FakeFactory, SimulatedCrash


def stub_planner(tool="restart_sync", procedure_id="proc_sync_restart"):
    async def planner(health):
        return {
            "proposal": {
                "tool": tool,
                "machine_id": health["machine_id"],
                "procedure_id": procedure_id,
                "rationale": "stub planner",
            },
            "planner_mode": "stub",
            "retrieval_mode": "stub",
            "fallback_reason": None,
        }

    return planner


async def raising_planner(health):
    raise RuntimeError("planner down")


async def _run(run_id):
    return await storage.agent_runs().find_one({"_id": run_id})


async def _events(run_id, event_type):
    return await storage.audit_events().count_documents(
        {"run_id": run_id, "event_type": event_type}
    )


@pytest.mark.asyncio
async def test_queued_run_completes(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    assert await worker.poll_once("w1", factory, stub_planner()) == run_id
    run = await _run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["lease_owner"] is None and run["lease_expires_at"] is None
    assert run["retrieval_mode"] == "stub"
    assert run["recovery_attempts"] == 0
    assert factory.restart_count == 1
    assert await _events(run_id, "PLANNER_RESULT") == 1


@pytest.mark.asyncio
async def test_lease_is_exclusive(ready):
    run_id = await storage.enqueue_run("machine_b")
    a, b = await asyncio.gather(
        ledger.acquire_run_lease("worker_a", worker.LEASE_SECONDS),
        ledger.acquire_run_lease("worker_b", worker.LEASE_SECONDS),
    )
    winners = [r for r in (a, b) if r is not None]
    assert len(winners) == 1
    assert winners[0]["_id"] == run_id
    # while the lease is live, nobody else gets it
    assert await worker.poll_once("worker_c", FakeFactory(), stub_planner()) is None
    # renewal works only for the holder
    holder = winners[0]["lease_owner"]
    other = "worker_b" if holder == "worker_a" else "worker_a"
    assert await ledger.renew_lease(run_id, holder, worker.LEASE_SECONDS) is True
    assert await ledger.renew_lease(run_id, other, worker.LEASE_SECONDS) is False


@pytest.mark.asyncio
async def test_terminal_runs_never_picked_up(ready):
    factory = FakeFactory()
    for status in worker.TERMINAL_RUN_STATUSES:
        run_id = await storage.enqueue_run("machine_b")
        await storage.agent_runs().update_one(
            {"_id": run_id},
            {"$set": {"status": status, "lease_owner": None,
                      "lease_expires_at": ledger.lease_time(-60)}},
        )
    assert await worker.poll_once("w1", factory, stub_planner()) is None
    assert factory.mutation_calls == []


@pytest.mark.asyncio
async def test_crash_recovery_does_not_repeat_restart(ready, monkeypatch):
    factory = FakeFactory()
    crashes = []

    def fake_crash():
        crashes.append(1)
        raise SimulatedCrash()

    monkeypatch.setattr(executor, "_crash", fake_crash)
    run_id = await storage.enqueue_run("machine_b", crash_armed=True)

    with pytest.raises(SimulatedCrash):
        await worker.poll_once("worker_a", factory, stub_planner())
    run = await _run(run_id)
    assert run["status"] == "RUNNING"
    assert run["crash_fired"] is True
    assert run["lease_owner"] == "worker_a"  # dead worker's lease is left to expire
    assert factory.restart_count == 1
    assert factory.fault["machine_b"] == "NONE"

    # lease still live: a new worker must not touch it
    assert await worker.poll_once("worker_b", factory, stub_planner()) is None

    await storage.agent_runs().update_one(
        {"_id": run_id}, {"$set": {"lease_expires_at": ledger.lease_time(-1)}}
    )
    assert await worker.poll_once("worker_b", factory, stub_planner()) == run_id

    run = await _run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["recovery_attempts"] == 1
    assert run["lease_owner"] is None
    (action,) = await ledger.get_machine_ledger(run_id)
    assert action["status"] == "EFFECT_VERIFIED"
    assert factory.restart_count == 1
    assert len(factory.mutation_calls) == 1
    assert crashes == [1]


@pytest.mark.asyncio
async def test_recovery_attempts_exceeded_fails_run(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    await storage.agent_runs().update_one(
        {"_id": run_id},
        {"$set": {"status": "NEEDS_RECONCILIATION", "recovery_attempts": 3,
                  "first_picked_at": ledger.lease_time(-120)}},
    )
    assert await worker.poll_once("w1", factory, stub_planner()) == run_id
    run = await _run(run_id)
    assert run["recovery_attempts"] == 4
    assert run["status"] == "FAILED"
    assert await _events(run_id, "RECOVERY_ATTEMPTS_EXCEEDED") == 1
    assert factory.mutation_calls == []
    # and a FAILED run is never picked up again
    assert await worker.poll_once("w2", factory, stub_planner()) is None


@pytest.mark.asyncio
async def test_denied_proposal_ends_run_denied(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    planner = stub_planner(tool="shutdown_factory", procedure_id="proc_shutdown")
    assert await worker.poll_once("w1", factory, planner) == run_id
    run = await _run(run_id)
    assert run["status"] == "DENIED"
    assert factory.mutation_calls == []
    assert factory.receipts == {}


@pytest.mark.asyncio
async def test_planner_failure_uses_labelled_local_fallback(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    assert await worker.poll_once("w1", factory, raising_planner) == run_id
    run = await _run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["retrieval_mode"] == "fallback_deterministic"
    assert run["planner_mode"] == "fallback_deterministic"
    assert await _events(run_id, "PLANNER_FALLBACK") == 1
    assert await _events(run_id, "PLANNER_RESULT") == 0
    assert factory.restart_count == 1


@pytest.mark.asyncio
async def test_healthy_machine_abstains_and_fails(ready):
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_a")  # fault NONE
    assert await worker.poll_once("w1", factory, raising_planner) == run_id
    run = await _run(run_id)
    assert run["status"] == "FAILED"
    assert await _events(run_id, "PLANNER_ABSTAINED") == 1
    assert factory.mutation_calls == []


# --- crash guard and planner trace ---------------------------------------------


def test_default_crash_raises_when_not_enabled(monkeypatch):
    monkeypatch.setattr(executor, "REAL_CRASH", False)
    assert issubclass(executor.CrashNotAllowedError, RuntimeError)
    with pytest.raises(executor.CrashNotAllowedError):
        executor._crash()


def test_worker_boot_enables_real_crash(monkeypatch):
    monkeypatch.setattr(executor, "REAL_CRASH", False)
    worker.enable_real_crash()
    assert executor.REAL_CRASH is True  # monkeypatch restores the original afterwards


def _trace_planner(monkeypatch, trace):
    """build_default_planner() wired to a fake agent.graph.propose_recovery_with_trace."""
    import agent.graph as agent_graph

    calls = []

    async def fake_trace(health, *, collection=None, gemini_client=None):
        calls.append(health["machine_id"])
        return trace

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # never reach the real model
    monkeypatch.setattr(agent_graph, "propose_recovery_with_trace", fake_trace, raising=False)
    planner, closer = worker.build_default_planner()
    assert planner is not None
    return planner, calls


@pytest.mark.asyncio
async def test_planner_uses_trace_and_records_evidence(ready, monkeypatch):
    planner, calls = _trace_planner(monkeypatch, {
        "proposal": {"tool": "restart_sync", "machine_id": "machine_b",
                     "procedure_id": "proc_sync_restart", "rationale": "vector hit"},
        "retrieval_mode": "atlas_vector",
        "score": 0.91,
    })
    factory = FakeFactory()
    run_id = await storage.enqueue_run("machine_b")
    assert await worker.poll_once("w1", factory, planner) == run_id
    run = await _run(run_id)
    assert calls == ["machine_b"]
    assert run["status"] == "COMPLETED"
    assert run["retrieval_mode"] == "atlas_vector"
    assert run["planner_mode"] == "gemini"
    assert run["retrieval_score"] == 0.91
    assert run["procedure_id"] == "proc_sync_restart"
    event = await storage.audit_events().find_one({"run_id": run_id, "event_type": "PLANNER_RESULT"})
    assert event["detail"]["score"] == 0.91
    assert factory.restart_count == 1


@pytest.mark.asyncio
async def test_planner_trace_fallback_is_labelled_honestly(ready, monkeypatch):
    planner, _ = _trace_planner(monkeypatch, {
        "proposal": {"tool": "restart_sync", "machine_id": "machine_b",
                     "procedure_id": "proc_sync_restart", "rationale": "fallback"},
        "retrieval_mode": "fallback_deterministic",
        "score": None,
    })
    run_id = await storage.enqueue_run("machine_b")
    assert await worker.poll_once("w1", FakeFactory(), planner) == run_id
    run = await _run(run_id)
    assert run["planner_mode"] == "fallback_deterministic"  # never claims the model ran
    assert run["retrieval_mode"] == "fallback_deterministic"
    assert run["retrieval_score"] is None


@pytest.mark.asyncio
async def test_planner_without_trace_function_keeps_plan_recovery_path(ready, monkeypatch):
    import agent.graph as agent_graph

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delattr(agent_graph, "propose_recovery_with_trace", raising=False)
    planner, _ = worker.build_default_planner()
    run_id = await storage.enqueue_run("machine_b")
    assert await worker.poll_once("w1", FakeFactory(), planner) == run_id
    run = await _run(run_id)
    assert run["status"] == "COMPLETED"
    # her plan_recovery says "deterministic_fallback"; the worker records one label
    assert run["retrieval_mode"] == "fallback_deterministic"
    assert run["planner_mode"] == "fallback_deterministic"
    assert run["retrieval_score"] is None
