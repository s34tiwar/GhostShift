"""Run control route adapters (create, resume, crash-arm). Owner: Enes.

Thin shells over Rhea's functions: reliability.storage.enqueue_run and
reliability.ledger.{arm_crash, resume_run}. No work happens inside the request;
the separate worker picks the QUEUED run up.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal

from api import factory_client
from api.schemas import CrashArmRequest, MachineHealth, MachineId, RunCreateRequest, RunCreateResponse
from reliability import ledger, storage

router = APIRouter(prefix="/api")


class SyncFailureRequest(BaseModel):
    machine_id: MachineId


class ResetRequest(BaseModel):
    confirm: Literal[True]
    # force=true aborts every active run (FAILED + RUN_ABORTED_BY_RESET audit) first.
    force: bool = False


@router.post("/scenarios/sync_failure", response_model=MachineHealth)
async def sync_failure(body: SyncFailureRequest):
    try:
        return await factory_client.break_sync(body.machine_id)
    except factory_client.FactoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except factory_client.FactoryResponseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/scenarios/reset")
async def reset_scenario(body: ResetRequest):
    aborted: list[str] = []
    if body.force:
        aborted = await ledger.abort_active_runs("demo reset")
    elif await ledger.has_active_runs():
        raise HTTPException(
            status_code=409,
            detail="a recovery run is still active; retry with force=true to abort it",
        )
    try:
        result = await factory_client.reset()
    except factory_client.FactoryUnavailable as exc:
        raise HTTPException(
            status_code=503, detail={"message": str(exc), "aborted_run_ids": aborted}
        ) from exc
    except factory_client.FactoryResponseError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"message": exc.detail, "aborted_run_ids": aborted}
        ) from exc
    return {**result, "aborted_run_ids": aborted}


@router.post("/runs", status_code=202, response_model=RunCreateResponse)
async def create_run(body: RunCreateRequest):
    # A demo scenario (e.g. unsafe_shutdown) may target a healthy machine; a normal
    # recovery run still needs a fault to recover.
    if body.scenario is None:
        try:
            health = await factory_client.get_health(body.machine_id)
        except factory_client.FactoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except factory_client.FactoryResponseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if health.get("healthy") and health.get("fault_type", "NONE") == "NONE":
            raise HTTPException(status_code=409, detail=f"{body.machine_id} has no fault to recover")
    extra = {"scenario": body.scenario} if body.scenario is not None else {}
    try:
        run_id = await storage.enqueue_run(body.machine_id, body.crash_armed, **extra)
    except storage.ActiveRunExistsError as exc:
        # The message names the blocking run when the exception carries its id.
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
