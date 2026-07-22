"""Liveness/readiness + hello-world (the P0.0 CI target)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from relay import __version__
from relay.core.db import db_healthcheck

router = APIRouter(tags=["system"])


class Health(BaseModel):
    status: str
    version: str


class Readiness(BaseModel):
    status: str
    checks: dict[str, bool]


class Hello(BaseModel):
    message: str
    service: str


@router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness: the process is up. Cheap, no dependencies."""
    return Health(status="ok", version=__version__)


@router.get("/readyz", response_model=Readiness)
async def readyz() -> Readiness:
    """Readiness: dependencies reachable. Used by orchestration before routing traffic."""
    db_ok = await db_healthcheck()
    checks = {"database": db_ok}
    return Readiness(status="ok" if all(checks.values()) else "degraded", checks=checks)


@router.get("/v0/hello", response_model=Hello)
async def hello() -> Hello:
    """Hello-world endpoint — the P0.0 acceptance target for a green CI pipeline."""
    return Hello(message="Hello from Relay", service="relay-api")
