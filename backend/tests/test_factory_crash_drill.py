"""Real-process crash drills for the factory. Owner: Arav (LiveFactory is reusable by Rhea).

These start a real uvicorn factory process and real "worker" processes that die with
os._exit(42), then check that a resumer reconciles from MongoDB without repeating the repair.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest

from .conftest import REPO_ROOT, TEST_DB_NAME

OP = "run_drill:machine_b:restart_sync:epoch_1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveFactory:
    """Runs `uvicorn backend.factory.main:app` as a real process against a given collection."""

    def __init__(self, collection_name: str, db_name: str = TEST_DB_NAME, token: str = ""):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "MONGODB_URI": os.environ["TEST_MONGODB_URI"],
            "MONGODB_DB": db_name,
            "FACTORY_COLLECTION": collection_name,
            "FACTORY_TOKEN": token,
        }
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 45) -> "LiveFactory":
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.factory.main:app", "--port", str(self.port), "--log-level", "warning"],
            cwd=REPO_ROOT, env=self.env,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"factory exited early with code {self.proc.returncode}")
            try:
                if httpx.get(f"{self.url}/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("factory did not become healthy in time")

    def hard_kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no graceful shutdown
            self.proc.wait(timeout=15)

    def restart(self) -> "LiveFactory":
        self.hard_kill()
        return self.start()


@pytest.fixture
def live(collection):
    factory = LiveFactory(collection.name).start()
    yield factory
    factory.hard_kill()


def run_worker(url: str, die_after_effect: bool) -> int:
    """A stand-in for Rhea's worker: optionally applies the repair, then dies with os._exit(42)."""
    code = textwrap.dedent(f"""
        import os, httpx
        if {die_after_effect!r}:
            r = httpx.post("{url}/machines/machine_b/restart-sync", json={{"operation_id": "{OP}"}}, timeout=20)
            assert r.json()["applied"] is True
        os._exit(42)  # crash before verification
    """)
    return subprocess.run([sys.executable, "-c", code], timeout=60).returncode


def test_worker_dies_after_effect_resumer_does_not_repeat(live):
    httpx.post(f"{live.url}/machines/machine_b/break", json={"fault_type": "SYNC_PROCESS_DOWN"}, timeout=20)
    assert run_worker(live.url, die_after_effect=True) == 42

    # Resumer reconciles: verify first, sees the effect already present.
    v = httpx.get(f"{live.url}/machines/machine_b/verify", params={"expected_action": "restart_sync", "operation_id": OP}, timeout=20).json()
    assert v["effect_present"] is True and v["restart_count"] == 1
    # Even if it blindly retries with the same operation_id, nothing repeats.
    r = httpx.post(f"{live.url}/machines/machine_b/restart-sync", json={"operation_id": OP}, timeout=20).json()
    assert r["replayed"] is True and r["restart_count"] == 1


def test_worker_dies_before_effect_resumer_applies_once(live):
    httpx.post(f"{live.url}/machines/machine_b/break", json={"fault_type": "SYNC_PROCESS_DOWN"}, timeout=20)
    assert run_worker(live.url, die_after_effect=False) == 42

    v = httpx.get(f"{live.url}/machines/machine_b/verify", params={"expected_action": "restart_sync", "operation_id": OP}, timeout=20).json()
    assert v["effect_present"] is False and v["receipt_exists"] is False
    r = httpx.post(f"{live.url}/machines/machine_b/restart-sync", json={"operation_id": OP}, timeout=20).json()
    assert r["applied"] is True and r["replayed"] is False and r["restart_count"] == 1


def test_factory_hard_kill_keeps_state_and_receipts(live):
    httpx.post(f"{live.url}/machines/machine_b/break", json={"fault_type": "SYNC_PROCESS_DOWN"}, timeout=20)
    httpx.post(f"{live.url}/machines/machine_c/break", json={"fault_type": "NETWORK_DISCONNECTED"}, timeout=20)
    httpx.post(f"{live.url}/machines/machine_b/restart-sync", json={"operation_id": OP}, timeout=20)

    live.restart()

    c = httpx.get(f"{live.url}/machines/machine_c/health", timeout=20).json()
    assert c["healthy"] is False and c["fault_type"] == "NETWORK_DISCONNECTED"  # restart didn't reset it
    r = httpx.post(f"{live.url}/machines/machine_b/restart-sync", json={"operation_id": OP}, timeout=20).json()
    assert r["replayed"] is True and r["restart_count"] == 1
    v = httpx.get(f"{live.url}/machines/machine_b/verify", params={"expected_action": "restart_sync"}, timeout=20).json()
    assert v["effect_present"] is True


def test_six_simultaneous_resumers_apply_once(live):
    httpx.post(f"{live.url}/machines/machine_b/break", json={"fault_type": "SYNC_PROCESS_DOWN"}, timeout=20)
    results = []
    barrier = threading.Barrier(6)

    def resumer():
        barrier.wait()
        results.append(httpx.post(f"{live.url}/machines/machine_b/restart-sync", json={"operation_id": OP}, timeout=30).json())

    threads = [threading.Thread(target=resumer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert httpx.get(f"{live.url}/machines/machine_b/health", timeout=20).json()["restart_count"] == 1


def test_demo_reset_refuses_while_run_active(live, test_db):
    runs = test_db[f"{live.env['FACTORY_COLLECTION']}_runs"]
    try:
        httpx.post(f"{live.url}/machines/machine_b/break", json={"fault_type": "SYNC_PROCESS_DOWN"}, timeout=20)
        runs.insert_one({"run_id": "run_active", "status": "EXECUTING"})
        env = {**live.env, "FACTORY_BASE_URL": live.url, "RUNS_COLLECTION": runs.name}
        script = str(REPO_ROOT / "scripts" / "demo_reset.py")

        assert subprocess.run([sys.executable, script], env=env, timeout=60).returncode == 2
        assert httpx.get(f"{live.url}/machines/machine_b/health", timeout=20).json()["healthy"] is False

        runs.update_one({"run_id": "run_active"}, {"$set": {"status": "COMPLETED"}})
        assert subprocess.run([sys.executable, script], env=env, timeout=60).returncode == 0
        assert httpx.get(f"{live.url}/machines/machine_b/health", timeout=20).json()["healthy"] is True
    finally:
        runs.drop()


def test_factory_refuses_to_start_without_mongodb_uri(collection):
    factory = LiveFactory(collection.name)
    factory.env["MONGODB_URI"] = ""
    with pytest.raises(RuntimeError, match="exited early"):
        factory.start(timeout=30)
