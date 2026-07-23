"""Liveness/readiness + hello-world (the P0.0 CI target)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import Response

from relay import __version__
from relay.core.db import db_healthcheck

router = APIRouter(tags=["system"])


async def _redis_healthcheck() -> bool:
    from relay.core.redis import get_redis

    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


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
    redis_ok = await _redis_healthcheck()
    checks = {"database": db_ok, "redis": redis_ok}
    return Readiness(status="ok" if all(checks.values()) else "degraded", checks=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Prometheus exposition for the `app` shape (RFC-001 §9). Excluded from the OpenAPI/SDK
    contract (``include_in_schema=False``) so it never affects the generated client."""
    from relay.core.observability import metrics as m

    m.refresh_runtime_gauges()
    data, content_type = m.render_latest()
    return Response(content=data, media_type=content_type)


@router.get("/v0/hello", response_model=Hello)
async def hello() -> Hello:
    """Hello-world endpoint — the P0.0 acceptance target for a green CI pipeline."""
    return Hello(message="Hello from Relay", service="relay-api")
