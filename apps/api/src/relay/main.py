"""FastAPI application assembly (RFC-001 §6.1 — the `app` runtime shape).

The public API is versioned (``/v0``). Feature modules expose an ``APIRouter`` as
``relay.modules.<name>.router`` and are mounted here; nothing else crosses module lines
at the HTTP layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from relay import __version__, health
from relay.core.errors import register_exception_handlers
from relay.core.logging import configure_logging, get_logger
from relay.core.middleware import RequestContextMiddleware
from relay.modules.crm.router import router as crm_router
from relay.modules.identity.middleware import TenancyMiddleware
from relay.modules.identity.router import router as identity_router
from relay.modules.messaging.router import router as messaging_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("relay.api.startup", version=__version__)
    yield
    log.info("relay.api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Relay API",
        version=__version__,
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )

    # Middleware runs outermost-first; add_middleware stacks in reverse, so
    # RequestContextMiddleware (added last) wraps TenancyMiddleware.
    app.add_middleware(TenancyMiddleware)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    # System + hello-world.
    app.include_router(health.router)

    # Feature modules (versioned public API).
    app.include_router(identity_router, prefix="/v0")
    app.include_router(crm_router, prefix="/v0")
    app.include_router(messaging_router, prefix="/v0")

    return app


app = create_app()
