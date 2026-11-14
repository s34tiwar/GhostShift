# GhostShift

**A self-healing factory agent that fixes a broken machine safely, survives a mid-repair crash without repeating the fix, and is blocked from doing anything it isn't allowed to do.**

MongoDB "Agents in Context" Hackathon, Toronto, Nov 14, 2026. Track: *Finish the Job* (secondary: *Agents You Can Audit*).

All data is synthetic. The factory is a simulation, but its state is real and durable in MongoDB Atlas.

## What it does

A simulated factory has three machines (`machine_a`, `machine_b`, `machine_c`) that sync production data. From the UI you can break Machine B's sync, then let the recovery agent fix it:

1. **Detect:** the agent reads Machine B's real health from the factory.
2. **Find a fix:** it retrieves a past recovery procedure (Atlas Vector Search, with a deterministic fallback) and a Gemini planner proposes `restart_sync`.
3. **Policy gate:** the proposal is checked against policies stored in MongoDB *before* anything is changed.
4. **Durable action ledger:** intent is recorded first, then the factory repair runs with an idempotent `operation_id`.
5. **Verify:** the result is checked independently against the factory. Only then is the action marked `EFFECT_VERIFIED`.
6. **Show:** the UI polls machine state, the run, its audit events and the ledger once per second and drives the six-step progress panel from them. A machine shows as *Reconciled* only after the run is `COMPLETED` and the effect is verified.

### The three demo cases

| Case | How to start it | Expected result |
|---|---|---|
| **1. Normal recovery** | **Stop sync**, then **Run recovery** | Machine B goes unhealthy, the run goes `QUEUED` → `DIAGNOSING` → `ACTION_PENDING` → `RUNNING` → `COMPLETED`, Machine B is healthy again, factory `restart_count` is exactly 1. |
| **2. Crash and resume** | Stop sync, then create a run with `crash_armed: true` (API call, see below) | The worker kills itself (`os._exit(42)`) after the repair but before verification. A restarted worker takes over the run after the 10 s lease expires, sees the repair already happened, verifies it, and completes. Same `run_id`, same `operation_id`, `restart_count` stays 1. |
| **3. Unsafe action** | **Try unsafe action** | The agent proposes `shutdown_factory`. The policy check denies it, the run ends `DENIED`, the audit shows the denial, and the factory is never called. Machine B's `restart_count` does not change. |

**Reset** restores all machines to healthy. If a run is stuck (for example the worker was killed on purpose), Reset asks for confirmation and force-aborts it. Run and audit history is never deleted.

## Architecture

```
Browser ── Vercel / Next.js (frontend/)
              │  HTTPS + CORS, 1-second polling (no WebSocket)
          API (FastAPI, backend/api)   ── read + run-control routes
              │                │
              │                └── factory client ──► Factory (FastAPI, backend/factory)
              ▼                                          machine state + idempotent receipts
        MongoDB Atlas  ◄──────────────────────────────── (own collection: factory_machines)
        agent_runs · action_ledger · audit_events · policies · recovery_procedures
              ▲
        Worker (separate process, backend/reliability/worker.py)
        LangGraph planner (backend/agent) → policy → claim → effect → verify
        lease owner + expiry; controlled crash and takeover
```

- The API and the worker are **separate processes**. The worker is never a FastAPI background task.
- The API never reads Mongo directly for runs: it calls the `reliability` module (`get_run`, `list_audit`, `get_machine_ledger`, `enqueue_run`, `resume_run`, `arm_crash`).
- Factory state lives in MongoDB (not SQLite), in its own collection.


## Repo layout

```
frontend/            Next.js control center (App Router, TypeScript)
backend/api/         FastAPI routes: read_routes, scenario_routes, factory_client, schemas
backend/factory/     Factory simulator (see backend/factory/README.md)
backend/reliability/ storage, ledger, policies, executor, worker
backend/agent/       LangGraph planner, retrieval, seed procedures
backend/tests/       pytest suites
scripts/             seed.py (vector index + procedures), demo_reset.py
```

## Run it locally

Requirements: Python 3.10 or newer, Node 20 or newer, a MongoDB Atlas cluster.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in the values, never commit .env
python scripts/seed.py        # one-time: vector index + recovery procedures (needs GEMINI_API_KEY)
```

Start the four processes in separate terminals. Load `.env` into each (for example `set -a; source .env; set +a`).

```bash
# 1. Factory (from repo root)
uvicorn backend.factory.main:app --port 8001

# 2. API (from backend/)
cd backend && uvicorn api.main:app --port 8000

# 3. Worker (from backend/)
cd backend && python -m reliability.worker

# 4. Frontend (from frontend/)
cd frontend && NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm install && npm run dev
```

Open http://localhost:3000. The header badge reads **Live backend** when the API URL is set.

For local runs, set `FACTORY_BASE_URL=http://localhost:8001` and `CORS_ORIGINS=http://localhost:3000`.

### Running the crash demo (case 2)

Start the run with an API call:

```bash
# after clicking Stop sync on Machine B
curl -X POST http://localhost:8000/api/runs \
  -H 'content-type: application/json' \
  -d '{"machine_id":"machine_b","crash_armed":true}'
```

The worker process will exit with code 42 right after the repair. **Restart it** (`python -m reliability.worker`, or let your platform's restart policy do it). After the 10 s lease expires, the new worker takes the run over and completes it. You can also force it with `POST /api/runs/{run_id}/resume`.

## API

Base URL is `NEXT_PUBLIC_API_BASE_URL`. Schemas are in `backend/api/schemas.py`.

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{ok: true}` |
| GET | `/api/machines` | Proxies factory health for all machines |
| POST | `/api/scenarios/sync_failure` | `{machine_id}` breaks that machine's sync (real change in MongoDB) |
| POST | `/api/scenarios/reset` | `{confirm: true, force?: bool}`. 409 while a run is active unless `force` is true; `force` aborts active runs (status `FAILED`, audit event `RUN_ABORTED_BY_RESET`) and then resets |
| POST | `/api/runs` | `{machine_id, crash_armed?, scenario?}` returns 202 `{run_id, status:"QUEUED"}`. 409 if the machine has no fault, or already has an active run. 503 if the factory is unreachable. `scenario` is optional, only `unsafe_shutdown` |
| GET | `/api/runs/{id}` | `status, machine_id, procedure_id, retrieval_mode, current_action_id, restart_count` (manual resumes), `recovery_count` (worker takeovers), `worker_status` |
| GET | `/api/runs/{id}/audit` | `{events: [{at, actor, event_type, reason, action_id, policy_version}]}` in time order |
| GET | `/api/runs/{id}/actions` | The action ledger for the run |
| POST | `/api/runs/{id}/resume` | Moves an unfinished run to `NEEDS_RECONCILIATION`. Same `run_id`. No-op for finished runs |
| POST | `/api/demo/crash-arm` | `{run_id, stage:"after_effect_before_verify"}`, one-shot per run |

Run statuses: `QUEUED`, `CREATED`, `DIAGNOSING`, `ACTION_PENDING`, `RUNNING`, `NEEDS_RECONCILIATION`, `COMPLETED`, `FAILED`, `DENIED`.
Action statuses: `PLANNED`, `AUTHORIZED`, `EXECUTING`, `EFFECT_VERIFIED`, `DENIED`, `FAILED`, `REQUIRES_REVIEW`.

## Configuration

| Variable | Used by | Purpose |
|---|---|---|
| `BACKEND_MONGODB_URI` | API, worker, factory | Atlas connection string (legacy `MONGODB_URI` also accepted) |
| `MONGODB_DB` | API, worker, factory | Database name |
| `FACTORY_BASE_URL` | API, worker | Public URL of the factory service |
| `FACTORY_TOKEN` | factory, API, worker | Optional. If set on the factory, every POST needs header `X-Factory-Token` |
| `FACTORY_COLLECTION` | factory | Collection for machine state (default `factory_machines`) |
| `CORS_ORIGINS` | API | Comma-separated allowed origins, the exact Vercel URL in production |
| `GEMINI_API_KEY` | worker, seed script | LLM planner and embeddings. Never in the frontend |
| `LLM_MODEL`, `EMBEDDING_MODEL`, `VECTOR_INDEX_NAME` | worker, seed script | Model names and vector index name |
| `NEXT_PUBLIC_API_BASE_URL` | frontend | API URL. Baked in at build time. If empty, the page runs a clearly labelled browser-only preview |

No secrets belong in the repo or in any `NEXT_PUBLIC_` variable.

## Deploy

- **Factory** (repo root): `uvicorn backend.factory.main:app --host 0.0.0.0 --port $PORT`
- **API** (from `backend/`): `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- **Worker** (from `backend/`): `python -m reliability.worker`. **Set a restart-on-exit policy**, otherwise the crash demo's worker stays dead.
- **Frontend** (Vercel): Root Directory `frontend`, env `NEXT_PUBLIC_API_BASE_URL=<API URL>` (no trailing slash). Then set the API's `CORS_ORIGINS` to the Vercel URL and redeploy the API. Rebuild the frontend after changing `NEXT_PUBLIC_API_BASE_URL`.

## Tests

```bash
cd backend && python -m pytest tests -q
```

Tests that need MongoDB skip themselves when the connection is not configured: set `BACKEND_MONGODB_URI` (or `MONGODB_URI`) for the API live tests, and `TEST_MONGODB_URI` for the factory tests. They use throwaway databases and collections and clean up after themselves.

## Technologies

- **Data:** MongoDB Atlas (Sandbox), Atlas Vector Search
- **Backend:** Python, FastAPI, Uvicorn, PyMongo
- **Agent:** LangGraph, Google Gemini API
- **Frontend:** Next.js, React, TypeScript, Tailwind CSS
- **Hosting:** Vercel (frontend), Railway (API, factory, worker)
