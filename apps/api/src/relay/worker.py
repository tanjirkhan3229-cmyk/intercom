"""Celery application (RFC-001 §6.4 — the segregated `workers` runtime shape).

Queues are bulkheads: bursty/slow work must never starve the interactive path. Every
task is idempotent (at-least-once delivery); DLQ + bounded retries are the default.
Actual tasks are registered by feature modules as they are built.
"""

from __future__ import annotations

from celery import Celery
from kombu import Queue

from relay.core.logging import configure_logging
from relay.settings import get_settings

settings = get_settings()

# Bulkhead queues (RFC-001 §6.4). `interactive` is the default so nothing slow lands there
# by accident — slow/bursty work must opt into its own queue explicitly.
QUEUES = (
    "interactive",
    "ai.interactive",
    "ai.batch",
    "ingest",
    "send.email",
    "send.channels",
    "webhooks",
    "analytics",
    "housekeeping",
)

celery_app = Celery(
    "relay",
    broker=settings.redis_broker_url,
    backend=settings.redis_cache_url,
)

celery_app.conf.update(
    task_default_queue="interactive",
    task_queues=tuple(Queue(name) for name in QUEUES),
    task_acks_late=True,  # redeliver on worker crash (tasks are idempotent)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # fair dispatch; long tasks don't hog prefetch
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    # Feature modules add their task modules here as they land, e.g.:
    # include=["relay.modules.messaging.tasks", ...]
)


@celery_app.on_after_configure.connect  # type: ignore[misc]
def _setup_logging(**_kwargs: object) -> None:
    configure_logging()
