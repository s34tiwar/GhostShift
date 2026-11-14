"""Minimal async client for the factory service. Owner: Enes."""
from __future__ import annotations

import os

import httpx


class FactoryUnavailable(Exception):
    """Factory is not configured or could not be reached."""


class FactoryResponseError(Exception):
    """The factory rejected a valid API request."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def _request(method: str, path: str, body: dict | None = None):
    base = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    if not base:
        raise FactoryUnavailable("FACTORY_BASE_URL is not set")
    headers = {}
    if os.environ.get("FACTORY_TOKEN"):
        headers["x-factory-token"] = os.environ["FACTORY_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.request(method, f"{base}{path}", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise FactoryUnavailable(f"factory request failed: {exc}") from exc
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise FactoryResponseError(response.status_code, str(detail))
    return response.json()


async def get_health(machine_id: str) -> dict:
    return await _request("GET", f"/machines/{machine_id}/health")


async def list_machines() -> list[dict]:
    return await _request("GET", "/machines")


async def break_sync(machine_id: str) -> dict:
    return await _request("POST", f"/machines/{machine_id}/break", {"fault_type": "SYNC_PROCESS_DOWN"})


async def reset() -> dict:
    return await _request("POST", "/simulation/reset")
