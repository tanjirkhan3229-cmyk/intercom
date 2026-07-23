"""FastAPI application assembly (RFC-001 §6.1 — the `app` runtime shape).

The public API is versioned (``/v0``). Feature modules expose an ``APIRouter`` as
``relay.modules.<name>.router`` and are mounted here; nothing else crosses module lines
at the HTTP layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from relay import __version__, health
from relay.core.errors import register_exception_handlers
from relay.core.logging import configure_logging, get_logger
from relay.core.middleware import RequestContextMiddleware
from relay.core.observability import MetricsMiddleware, init_app_observability
from relay.core.public_api import PublicApiMiddleware
from relay.modules.ai.router import router as ai_router
from relay.modules.billing.router import router as billing_router
from relay.modules.channels.router import router as channels_router
from relay.modules.crm.router import router as crm_router
from relay.modules.identity.middleware import TenancyMiddleware
from relay.modules.identity.router import router as identity_router
from relay.modules.knowledge.router import router as knowledge_router
from relay.modules.messaging.router import router as messaging_router
from relay.modules.platform.router import router as platform_router
from relay.modules.reporting.router import router as reporting_router
from relay.modules.webhooks.router import router as webhooks_router

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

    # Middleware runs outermost-first; add_middleware stacks in reverse, so the last one added
    # wraps the earlier ones. CORS must be outermost to answer preflight before auth runs.
    # PublicApiMiddleware is added FIRST → innermost: it runs after TenancyMiddleware resolves the
    # principal, and no-ops for everything except API-key traffic (P0.11).
    app.add_middleware(PublicApiMiddleware)
    app.add_middleware(TenancyMiddleware)
    app.add_middleware(RequestContextMiddleware)
    # MetricsMiddleware sits just inside CORS: preflight OPTIONS answered by CORS aren't metered,
    # but every routed request is timed/counted (RFC-001 §9 golden signals).
    app.add_middleware(MetricsMiddleware)
    # The messenger widget embeds on any customer origin and the agent app runs on its own
    # domain — both call this API cross-origin, and the widget lead cookie needs credentialed
    # requests (so a wildcard origin won't do; we reflect the request origin instead).
    # ponytail: per-workspace origin allow-listing is the P1 hardening; egress here is a
    # low-privilege lead cookie + Bearer-token agent auth, so reflect-all is acceptable for now.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=[
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    )
    register_exception_handlers(app)

    # System + hello-world.
    app.include_router(health.router)

    # Feature modules (versioned public API).
    app.include_router(identity_router, prefix="/v0")
    app.include_router(crm_router, prefix="/v0")
    app.include_router(messaging_router, prefix="/v0")
    app.include_router(billing_router, prefix="/v0")
    app.include_router(knowledge_router, prefix="/v0")
    app.include_router(platform_router, prefix="/v0")
    app.include_router(channels_router, prefix="/v0")
    app.include_router(reporting_router, prefix="/v0")
    app.include_router(webhooks_router, prefix="/v0")
    app.include_router(ai_router, prefix="/v0")

    # Sentry + OTel tracing + FastAPI instrumentation (all no-ops unless configured — RFC-001 §9).
    init_app_observability(app)

    return app


app = create_app()
