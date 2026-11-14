"""Read-only run, audit, and ledger route adapters. Owner: Enes.

These are thin shells: every read goes through Rhea's reliability.ledger
functions. Nothing here queries MongoDB directly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.schemas import AuditResponse, LedgerEntry, RunView
from reliability import ledger

router = APIRouter(prefix="/api")


async def _require_run(run_id: str) -> dict:
    run = await ledger.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


@router.get("/runs/{run_id}", response_model=RunView)
async def read_run(run_id: str):
    return await _require_run(run_id)


@router.get("/runs/{run_id}/audit", response_model=AuditResponse)
async def read_audit(run_id: str):
    await _require_run(run_id)
    return await ledger.list_audit(run_id)


@router.get("/runs/{run_id}/actions", response_model=list[LedgerEntry])
async def read_actions(run_id: str):
    await _require_run(run_id)
    return await ledger.get_machine_ledger(run_id)
