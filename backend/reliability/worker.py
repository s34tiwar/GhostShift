"""Independent durable worker process and crash/resume loop. Owner: Rhea.

Run from backend/:  python -m reliability.worker

A separate OS process (never a FastAPI background task). Single replica is the
assumption; leases (owner + expiry) still prevent overlapping claims.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from uuid import uuid4

from reliability import executor, ledger, policies, storage

log = logging.getLogger("reliability.worker")

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1"))
LEASE_SECONDS = 10.0
HEARTBEAT_SECONDS = 3.0
MAX_RECOVERY_ATTEMPTS = 3

TERMINAL_RUN_STATUSES = ("COMPLETED", "FAILED", "DENIED")
WORKER_ACTOR = "worker"

# Local deterministic fallback: live fault -> (tool, procedure_id).
_FALLBACK_PLAN = {
    "SYNC_PROCESS_DOWN": ("restart_sync", "proc_sync_restart"),
    "NETWORK_DISCONNECTED": ("reconnect_network", "proc_network_reconnect"),
}
LOCAL_FALLBACK_MODE = "fallback_deterministic"


def _canonical_mode(mode):
    """Record one fallback label everywhere, whichever spelling the planner used."""
    return LOCAL_FALLBACK_MODE if mode == "deterministic_fallback" else mode


def new_owner_id() -> str:
    return f"worker_{os.getpid()}_{uuid4().hex[:8]}"


class LeaseLost(Exception):
    """This worker no longer holds the run's lease: stop touching the run."""


# --- lease guard -------------------------------------------------------------


class RunLease:
    """Holds one run's lease: a 3s heartbeat plus a renewal before each step."""

    def __init__(self, run_id: str, owner: str):
        self.run_id = run_id
        self.owner = owner
        self.lost = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _renew(self) -> bool:
        try:
            ok = await ledger.renew_lease(self.run_id, self.owner, LEASE_SECONDS)
        except Exception as exc:  # cannot confirm we still hold it: assume lost
            log.warning("lease renewal error on %s: %s", self.run_id, type(exc).__name__)
            ok = False
        if not ok:
            self.lost.set()
        return ok

    async def _heartbeat(self) -> None:
        while not self.lost.is_set():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._renew()

    def start(self) -> None:
        self._task = asyncio.create_task(self._heartbeat())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def checkpoint(self) -> None:
        """Call before every step. Raises LeaseLost if the run is no longer ours."""
        if self.lost.is_set() or not await self._renew():
            raise LeaseLost(self.run_id)


# --- planning ----------------------------------------------------------------


def local_fallback_proposal(health: dict) -> dict | None:
    plan = _FALLBACK_PLAN.get(health.get("fault_type"))
    if plan is None:
        return None
    tool, procedure_id = plan
    return {
        "tool": tool,
        "machine_id": health.get("machine_id"),
        "procedure_id": procedure_id,
        "rationale": (
            "Local deterministic fallback: live fault "
            f"{health.get('fault_type')} maps to {tool}. No LLM or vector search ran."
        ),
    }


def _valid_proposal(proposal, machine_id: str) -> bool:
    if not isinstance(proposal, dict):
        return False
    for field in ("tool", "machine_id", "procedure_id", "rationale"):
        if not isinstance(proposal.get(field), str) or not proposal[field]:
            return False
    return proposal["machine_id"] == machine_id


def build_default_planner():
    """Shivani's plan_recovery, wired to Atlas and Gemini when available.

    Returns (planner, closer). planner is None if her module cannot be imported.
    Without GEMINI_API_KEY her code takes its own labelled deterministic fallback.
    """
    try:
        import agent.graph as agent_graph

        plan_recovery = agent_graph.plan_recovery
    except Exception as exc:
        log.warning("agent planner unavailable: %s", type(exc).__name__)
        return None, None
    # Shivani's trace entry point, when her branch has landed. Optional.
    trace_fn = getattr(agent_graph, "propose_recovery_with_trace", None)

    gemini = None
    if os.environ.get("GEMINI_API_KEY"):
        try:
            from google import genai

            gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"]).aio
        except Exception as exc:
            log.warning("gemini client unavailable: %s", type(exc).__name__)
            gemini = None
    log.info("planner: %s", "gemini + atlas vector" if gemini else "agent deterministic fallback")

    collection = storage.get_db()["recovery_procedures"]

    async def planner(health: dict) -> dict:
        if trace_fn is not None:
            # Same clients as above, so her code never opens its own connections.
            trace = await trace_fn(health, collection=collection, gemini_client=gemini)
            retrieval_mode = trace.get("retrieval_mode")
            return {
                "proposal": trace.get("proposal"),
                # Only a real vector hit means the model path ran.
                "planner_mode": "gemini" if retrieval_mode == "atlas_vector" else LOCAL_FALLBACK_MODE,
                "retrieval_mode": retrieval_mode,
                "score": trace.get("score"),
                "fallback_reason": None,
            }
        result = await plan_recovery(health, collection=collection, gemini_client=gemini)
        return {
            "proposal": result.proposal.model_dump(),
            # Her plan_recovery labels its fallback "deterministic_fallback"; the
            # recorded label is always "fallback_deterministic" (same as her trace).
            "planner_mode": _canonical_mode(result.planner_mode),
            "retrieval_mode": _canonical_mode(result.retrieval.retrieval_mode),
            "score": result.retrieval.score,
            "fallback_reason": result.fallback_reason,
        }

    async def closer() -> None:
        if gemini is not None:
            await gemini.aclose()

    return planner, closer


async def _plan(run_id: str, machine_id: str, health: dict, planner, owner: str):
    """Return (proposal or None, retrieval_mode). Labels are always truthful."""
    reason = "agent planner unavailable"
    if planner is not None:
        try:
            outcome = await planner(health)
            proposal = outcome.get("proposal")
            if _valid_proposal(proposal, machine_id):
                await ledger.append_audit(
                    run_id, None, owner, "PLANNER_RESULT", None,
                    outcome.get("fallback_reason"),
                    {
                        "planner_mode": outcome.get("planner_mode"),
                        "retrieval_mode": outcome.get("retrieval_mode"),
                        "score": outcome.get("score"),
                        "tool": proposal["tool"],
                        "procedure_id": proposal["procedure_id"],
                    },
                    None,
                )
                await ledger.set_run_fields(
                    run_id,
                    planner_mode=outcome.get("planner_mode"),
                    retrieval_mode=outcome.get("retrieval_mode"),
                    retrieval_score=outcome.get("score"),
                    procedure_id=proposal["procedure_id"],
                )
                return proposal, outcome.get("retrieval_mode")
            reason = "agent planner returned an invalid proposal"
        except Exception as exc:
            reason = f"agent planner raised {type(exc).__name__}"

    proposal = local_fallback_proposal(health)
    if proposal is None:
        await ledger.append_audit(
            run_id, None, owner, "PLANNER_ABSTAINED", None,
            f"{reason}; no deterministic plan for fault {health.get('fault_type')!r}",
            {"fault_type": health.get("fault_type")}, None,
        )
        return None, None

    await ledger.append_audit(
        run_id, None, owner, "PLANNER_FALLBACK", None, reason,
        {"retrieval_mode": LOCAL_FALLBACK_MODE, "tool": proposal["tool"],
         "procedure_id": proposal["procedure_id"]},
        None,
    )
    await ledger.set_run_fields(
        run_id,
        planner_mode=LOCAL_FALLBACK_MODE,
        retrieval_mode=LOCAL_FALLBACK_MODE,
        retrieval_score=None,
        procedure_id=proposal["procedure_id"],
    )
    return proposal, LOCAL_FALLBACK_MODE


UNSAFE_SCENARIO = "unsafe_shutdown"
UNSAFE_DEMO_MODE = "demo_scenario"


async def _unsafe_demo_plan(run_id: str, machine_id: str, owner: str):
    """Demo scenario: deliberately propose an action the policy must deny."""
    proposal = {
        "tool": "shutdown_factory",
        "machine_id": machine_id,
        "procedure_id": "proc_unsafe_demo",
        "rationale": "Demo: unsafe action to show the policy gate",
    }
    await ledger.append_audit(
        run_id, None, owner, "PLANNER_RESULT", None,
        f"Demo scenario {UNSAFE_SCENARIO}: planner skipped, deliberately unsafe proposal",
        {"scenario": UNSAFE_SCENARIO, "planner_mode": UNSAFE_DEMO_MODE,
         "tool": proposal["tool"], "procedure_id": proposal["procedure_id"]},
        None,
    )
    await ledger.set_run_fields(
        run_id, planner_mode=UNSAFE_DEMO_MODE, retrieval_mode=None,
        retrieval_score=None, procedure_id=proposal["procedure_id"],
    )
    return proposal, None


# --- one run -----------------------------------------------------------------


async def _fail(run_id: str, owner: str, event_type: str, reason: str) -> None:
    await ledger.append_audit(run_id, None, owner, event_type, None, reason, {}, None)
    await ledger.set_run_status(run_id, "FAILED")


async def process_run(run: dict, owner: str, factory, planner) -> None:
    """One pass over a run this worker has just leased."""
    run_id = run["_id"]
    machine_id = run["machine_id"]
    lease = RunLease(run_id, owner)
    lease.start()
    try:
        if run.get("recovery_attempts", 0) > MAX_RECOVERY_ATTEMPTS:
            await lease.checkpoint()
            await _fail(run_id, owner, "RECOVERY_ATTEMPTS_EXCEEDED",
                        f"run picked up {run['recovery_attempts']} times after its first pickup")
        elif await ledger.get_machine_ledger(run_id):
            # Unfinished work first; never plan new work on top of it.
            await lease.checkpoint()
            await executor.reconcile_run(run_id, factory=factory, owner=owner)
        else:
            await lease.checkpoint()
            await ledger.set_run_status(run_id, "DIAGNOSING")
            if run.get("scenario") == UNSAFE_SCENARIO:
                # Demo: no health read, no planner. The proposal then goes through the
                # normal executor and policy path, which is what denies it.
                await lease.checkpoint()
                proposal, retrieval_mode = await _unsafe_demo_plan(run_id, machine_id, owner)
            else:
                await lease.checkpoint()
                health = await factory.health(machine_id)

                await lease.checkpoint()
                proposal, retrieval_mode = await _plan(run_id, machine_id, health, planner, owner)
            if proposal is None:
                await lease.checkpoint()
                await ledger.set_run_status(run_id, "FAILED")
            else:
                await lease.checkpoint()
                await ledger.set_run_status(
                    run_id, "ACTION_PENDING",
                    procedure_id=proposal["procedure_id"], retrieval_mode=retrieval_mode,
                )
                await lease.checkpoint()
                await executor.execute_guarded_action(
                    run_id, proposal, factory=factory, owner=owner
                )
    finally:
        await lease.stop()

    # Reached only without an exception: hand the run back. Owner-guarded, so a
    # lease this worker already lost (or that an abort cleared) is left untouched.
    await ledger.release_lease(run_id, owner)


async def poll_once(owner: str, factory, planner) -> str | None:
    """Lease and process at most one run. Exceptions propagate to the caller."""
    run = await ledger.acquire_run_lease(owner, LEASE_SECONDS)
    if run is None:
        return None
    await process_run(run, owner, factory, planner)
    return run["_id"]


# --- process entry -----------------------------------------------------------


def enable_real_crash() -> None:
    """Opt this process in to the demo crash. Only the worker process does this."""
    executor.REAL_CRASH = True


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    owner = new_owner_id()
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    enable_real_crash()

    factory = None
    closer = None
    try:
        await storage.init_indexes()
        await policies.seed_default_policy()
        factory = executor.get_factory()
        planner, closer = build_default_planner()
        log.info("worker %s started; polling every %ss", owner, POLL_INTERVAL)

        while not stop.is_set():
            try:
                run_id = await poll_once(owner, factory, planner)
                if run_id:
                    log.info("processed %s", run_id)
            except LeaseLost as exc:
                log.warning("lease lost on %s; stopped acting on it", exc)
            except ledger.RunTerminalError:
                log.warning("run finished or was aborted mid-pass; stopped acting on it")
            except Exception as exc:
                # Leave the lease to expire; another pass will pick the run up.
                log.error("run pass failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("worker %s shutting down", owner)
        if closer is not None:
            await closer()
        if factory is not None:
            await factory.aclose()
        await storage.close_client()


if __name__ == "__main__":
    asyncio.run(main())
