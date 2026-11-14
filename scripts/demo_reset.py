"""Reset synthetic factory demo state. Owners: Arav and Rhea.

Safety: refuses to reset while any run in Atlas `agent_runs` is still in flight, so a
rehearsal reset can't pull the rug out from under the worker. This script only READS
Atlas; the reset itself goes through the factory's HTTP API.

Usage (from repo root, env loaded):
    python scripts/demo_reset.py                 # check runs, then reset
    python scripts/demo_reset.py --force         # reset even if runs are active
    python scripts/demo_reset.py --skip-run-check  # don't contact Atlas at all

Exit codes: 0 reset done, 2 active runs (nothing reset), 3 Atlas could not be checked,
4 factory reset request failed.
Env: FACTORY_BASE_URL (default http://localhost:8001), FACTORY_TOKEN (optional),
MONGODB_URI, MONGODB_DB (default ghostshift), RUNS_COLLECTION (default agent_runs).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Both spellings accepted until the team settles on one (Rhea's enum vs. addendum diagram).
TERMINAL_STATUSES = {"COMPLETED", "COMPLETE", "FAILED", "DENIED"}


def active_runs() -> list[dict]:
    from pymongo import MongoClient

    uri = os.environ["MONGODB_URI"]
    db = os.environ.get("MONGODB_DB") or "ghostshift"
    col = os.environ.get("RUNS_COLLECTION") or "agent_runs"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        cursor = client[db][col].find(
            {"status": {"$nin": sorted(TERMINAL_STATUSES)}}, {"_id": 1, "run_id": 1, "status": 1}
        ).limit(20)
        return list(cursor)
    finally:
        client.close()


def reset_factory() -> dict:
    base = (os.environ.get("FACTORY_BASE_URL") or "http://localhost:8001").rstrip("/")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("FACTORY_TOKEN", "").strip()
    if token:
        headers["X-Factory-Token"] = token
    req = urllib.request.Request(f"{base}/simulation/reset", data=b"{}", headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="reset even if runs are still active")
    parser.add_argument("--skip-run-check", action="store_true", help="do not check Atlas for active runs")
    args = parser.parse_args(argv)

    if not args.skip_run_check:
        try:
            runs = active_runs()
        except Exception as exc:  # noqa: BLE001 - any failure means "could not check"
            print(f"Could not check Atlas for active runs: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Use --skip-run-check to reset anyway.", file=sys.stderr)
            return 3
        if runs and not args.force:
            print("Refusing to reset: these runs are still in flight:", file=sys.stderr)
            for r in runs:
                print(f"  {r.get('run_id') or r['_id']}  status={r.get('status')}", file=sys.stderr)
            print("Wait for them to finish, or pass --force.", file=sys.stderr)
            return 2

    try:
        result = reset_factory()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Factory reset failed: {exc}", file=sys.stderr)
        return 4
    states = ", ".join(f"{m['machine_id']}={'healthy' if m['healthy'] else m['fault_type']}" for m in result["machines"])
    print(f"Factory reset OK: {states}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
