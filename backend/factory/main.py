"""Factory FastAPI application and machine endpoints. Owner: Arav.

Run from the repo root:
    uvicorn backend.factory.main:app --port 8001
Env: MONGODB_URI (required), MONGODB_DB, FACTORY_COLLECTION, FACTORY_TOKEN (optional).
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import state
from .mongo_store import Contended, FactoryStore, OperationConflict, UnknownMachine


class BreakRequest(BaseModel):
    fault_type: Literal["SYNC_PROCESS_DOWN", "NETWORK_DISCONNECTED"]


class RepairRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=300, pattern=r"^\S+$")


def require_token(x_factory_token: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("FACTORY_TOKEN", "").strip()
    if not expected:
        return
    if not x_factory_token or not hmac.compare_digest(x_factory_token, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Factory-Token")


def create_app(store_factory: Callable[[], FactoryStore] = FactoryStore.from_env) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = store_factory()  # raises (and the process exits) if MONGODB_URI is missing
        store.ensure_machines()
        app.state.store = store
        yield

    app = FastAPI(title="GhostShift Factory Simulator", lifespan=lifespan)

    def store_of(request: Request) -> FactoryStore:
        return request.app.state.store

    @app.exception_handler(UnknownMachine)
    async def _unknown(_: Request, exc: UnknownMachine):
        return JSONResponse(status_code=404, content={"detail": f"unknown machine: {exc}"})

    @app.exception_handler(OperationConflict)
    async def _conflict(_: Request, exc: OperationConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Contended)
    async def _contended(_: Request, exc: Contended):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    def service_health():
        return {"ok": True, "service": "factory"}

    @app.get("/machines")
    def list_machines(store: FactoryStore = Depends(store_of)):
        return store.list_machines()

    @app.get("/machines/{machine_id}/health")
    def machine_health(machine_id: str, store: FactoryStore = Depends(store_of)):
        return store.health(machine_id)

    @app.post("/machines/{machine_id}/break", dependencies=[Depends(require_token)])
    def break_machine(machine_id: str, body: BreakRequest, store: FactoryStore = Depends(store_of)):
        return store.break_machine(machine_id, body.fault_type)

    @app.post("/machines/{machine_id}/restart-sync", dependencies=[Depends(require_token)])
    def restart_sync(machine_id: str, body: RepairRequest, store: FactoryStore = Depends(store_of)):
        return store.repair(machine_id, state.ACTION_RESTART_SYNC, body.operation_id)

    @app.post("/machines/{machine_id}/reconnect", dependencies=[Depends(require_token)])
    def reconnect(machine_id: str, body: RepairRequest, store: FactoryStore = Depends(store_of)):
        return store.repair(machine_id, state.ACTION_RECONNECT, body.operation_id)

    @app.get("/machines/{machine_id}/verify")
    def verify(
        machine_id: str,
        expected_action: Literal["restart_sync", "reconnect"] = Query(...),
        operation_id: Optional[str] = Query(default=None, max_length=300),
        store: FactoryStore = Depends(store_of),
    ):
        return store.verify(machine_id, expected_action, operation_id or None)

    @app.post("/simulation/reset", dependencies=[Depends(require_token)])
    def reset(store: FactoryStore = Depends(store_of)):
        return {"ok": True, "machines": store.reset()}

    return app


app = create_app()
