"""Minimal async client for the factory service. Owner: Enes."""
from __future__ import annotations

import os

import httpx


class FactoryUnavailable(Exception):
    """Factory is not configured or could not be reached."""


async def get_health(machine_id: str) -> dict:
    base = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    if not base:
        raise FactoryUnavailable("FACTORY_BASE_URL is not set")
    headers = {}
    if os.environ.get("FACTORY_TOKEN"):
        headers["x-factory-token"] = os.environ["FACTORY_TOKEN"]  # header name: confirm with Arav
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/machines/{machine_id}/health", headers=headers)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise FactoryUnavailable(f"factory request failed: {exc}") from exc
