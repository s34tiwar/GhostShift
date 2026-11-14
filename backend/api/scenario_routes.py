"""Run control route adapters (create, resume, crash-arm). Owner: Enes.

Thin shells over Rhea's functions: reliability.storage.enqueue_run and
reliability.ledger.{arm_crash, resume_run}. No work happens inside the request;
the separate worker picks the QUEUED run up.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api import factory_client
from api.schemas import CrashArmRequest, RunCreateRequest, RunCreateResponse
from reliability import ledger, storage

router = APIRouter(prefix="/api")


@router.post("/runs", status_code=202, response_model=RunCreateResponse)
async def create_run(body: RunCreateRequest):
    try:
        health = await factory_client.get_health(body.machine_id)
    except factory_client.FactoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if health.get("healthy") and health.get("fault_type", "NONE") == "NONE":
        raise HTTPException(status_code=409, detail=f"{body.machine_id} has no fault to recover")
    try:
        run_id = await storage.enqueue_run(body.machine_id, body.crash_armed)
    except storage.ActiveRunExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"run_id": run_id, "status": "QUEUED"}


@router.post("/runs/{run_id}/resume")
async def resume(run_id: str):
    try:
        return await ledger.resume_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/demo/crash-arm")
async def crash_arm(body: CrashArmRequest):
    try:
        return await ledger.arm_crash(body.run_id, body.stage)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
