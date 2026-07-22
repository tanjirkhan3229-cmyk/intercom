"""Structured logging (RFC-001 §9): JSON logs carrying request/workspace correlation ids.

Console renderer in development, JSON everywhere else. Call ``configure_logging()`` once
at process start (API, worker, beat).
"""

from __future__ import annotations

import logging

import structlog

from relay.core.context import request_id_var, workspace_id_var
from relay.settings import get_settings


def _inject_context(_logger: object, _name: str, event: dict[str, object]) -> dict[str, object]:
    """Merge request/workspace ids into every log line when present."""
    rid = request_id_var.get()
    wid = workspace_id_var.get()
    if rid is not None:
        event.setdefault("request_id", rid)
    if wid is not None:
        event.setdefault("workspace_id", str(wid))
    return event


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, format="%(message)s")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
