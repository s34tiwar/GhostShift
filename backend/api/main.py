"""FastAPI application entry point. Owner: Rhea; integration: Enes."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import read_routes, scenario_routes

app = FastAPI(title="GhostShift API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(read_routes.router)
app.include_router(scenario_routes.router)


@app.get("/health")
async def health():
    return {"ok": True}
