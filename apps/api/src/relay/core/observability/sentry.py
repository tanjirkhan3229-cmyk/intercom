"""Sentry error tracking (RFC-001 §9/§13).

A no-op unless ``SENTRY_DSN`` is set. When on, the FastAPI/Starlette/Celery integrations capture
unhandled errors with request + workspace context (added by our contextvars), the release is the
deploy SHA (ties errors to a canary), and every event is run through :func:`sentry_before_send`
so PII/secrets are scrubbed before it leaves the process.
"""

from __future__ import annotations

from relay.core.logging import get_logger
from relay.core.observability.scrub import sentry_before_send
from relay.settings import get_settings

log = get_logger(__name__)

_configured = False


def configure_sentry() -> bool:
    """Initialise Sentry if a DSN is configured. Idempotent; returns whether Sentry is active."""
    global _configured
    settings = get_settings()
    if not settings.sentry_dsn or _configured:
        return _configured

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.deploy_sha,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[StarletteIntegration(), FastApiIntegration(), CeleryIntegration()],
        before_send=sentry_before_send,  # type: ignore[arg-type]
        send_default_pii=False,
    )
    _configured = True
    log.info("sentry.configured", environment=settings.environment, release=settings.deploy_sha)
    return True
