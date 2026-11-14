# Factory simulator (owner: Arav)

Simulated machines with real, durable state in **MongoDB Atlas** (not SQLite: no volume needed).

## Run
```
uvicorn backend.factory.main:app --port 8001                       # local, from repo root
uvicorn backend.factory.main:app --host 0.0.0.0 --port $PORT       # Railway start command
```
Env: `BACKEND_MONGODB_URI` (required; legacy `MONGODB_URI` is also accepted), `MONGODB_DB` (default `ghostshift`),
`FACTORY_COLLECTION` (default `factory_machines`), `FACTORY_TOKEN` (optional: if set, every POST needs header
`X-Factory-Token`; GETs stay open).

Machines: `machine_a`, `machine_b`, `machine_c`. Faults: `NONE`, `SYNC_PROCESS_DOWN`, `NETWORK_DISCONNECTED`.

## Endpoints
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/health` | | `{ok: true, service: "factory"}` |
| GET | `/machines` | | list of machine health |
| GET | `/machines/{id}/health` | | `machine_id, healthy, fault_type, sync_running, network_connected, backlog, fault_epoch, last_operation_id, restart_count, reconnect_count, message` |
| POST | `/machines/{id}/break` | `{fault_type}` | machine health; `fault_epoch` +1, `backlog` = 125 |
| POST | `/machines/{id}/restart-sync` | `{operation_id}` | repair result (below) |
| POST | `/machines/{id}/reconnect` | `{operation_id}` | repair result (below) |
| GET | `/machines/{id}/verify` | `expected_action=restart_sync\|reconnect_network`, optional `operation_id` | verify result (below) |
| POST | `/simulation/reset` | | `{ok: true, machines: [...]}` |

**Repair result:** `operation_id, machine_id, action_type, applied, replayed, effect_present, reason, fault_epoch,
fault_type, healthy, restart_count, reconnect_count`. `action_type` is `restart_sync` or `reconnect_network`
(names match `ActionType` in `backend/api/schemas.py`). `reason` is `applied`, `wrong_action_for_fault` or `already_healthy`.

**Verify result:** `machine_id, effect_present, receipt_exists, receipt_applied, receipt_epoch, state_effect_present,
operation_id, healthy, fault_type, fault_epoch, restart_count, reconnect_count, expected_action`.

## Rules
- Same `operation_id` again: returns the saved receipt with `replayed: true`. No second repair, counters unchanged.
- Wrong repair (restart on a network fault, reconnect on a sync fault): HTTP 200, `applied: false`, fault stays, no counter change.
- Repair on a healthy machine: no-op (`applied: false`, `reason: already_healthy`).
- `operation_id` already used for another machine or action: **409**. Unknown machine **404**, bad input **422**,
  bad token **401**, too many lost races **503** (just retry).
- `verify.effect_present` is true only if an **applied receipt for the current `fault_epoch`** exists for that action
  AND the live state shows the repair. Without `operation_id`, the machine's `last_operation_id` is used.
  All booleans are real booleans; `receipt_epoch` is `null` when there is no receipt.
- Breaking a machine again starts a new epoch: old receipts no longer count, so use a new `operation_id`
  (`<run_id>:<machine_id>:<tool>:epoch_<n>`).
- A factory restart/redeploy never resets machines; only `/simulation/reset` does.

## Fault messages (stable; used in the vector query text)
- `NONE`: "Machine is healthy. Sync process running and network connected."
- `SYNC_PROCESS_DOWN`: "Sync process is not running. Production data is not being uploaded and the backlog is growing."
- `NETWORK_DISCONNECTED`: "Network connection lost. The machine cannot reach the data collector and the backlog is growing."

## Demo reset
`python scripts/demo_reset.py`: refuses (exit 2) while any run in `agent_runs` is not
`COMPLETED/FAILED/DENIED` (so `QUEUED` and in-progress runs block it); exit 3 if Atlas can't be checked. `--force`, `--skip-run-check`.
Uses `FACTORY_BASE_URL` (+ `FACTORY_TOKEN`).

## Tests
`python -m pytest backend/tests/test_factory.py backend/tests/test_factory_crash_drill.py` against a real MongoDB
(`TEST_MONGODB_URI`; read from `.env` automatically). Each test uses a throwaway collection in
`ghostshift_factory_tests` (override with `TEST_MONGODB_DB`). The crash drills start a real factory process and
worker processes that `os._exit(42)` mid-repair. `LiveFactory` in the drill file is reusable.
