"""Celery application (RFC-001 §6.4 — the segregated `workers` runtime shape).

Queues are bulkheads: bursty/slow work must never starve the interactive path. Every
task is idempotent (at-least-once delivery); DLQ + bounded retries are the default.
Actual tasks are registered by feature modules as they are built.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
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
    # Feature modules register their task modules here as they land.
    include=["relay.modules.crm.tasks", "relay.modules.messaging.tasks"],
)

# Durable timers / periodic housekeeping (RFC-001 §6.4, the `beat` runtime shape).
celery_app.conf.beat_schedule = {
    # Drain the analytics event buffers into the partitioned `events` table (W3).
    "crm-drain-events": {
        "task": "crm.drain_events",
        "schedule": 10.0,  # seconds
        "options": {"queue": "analytics"},
    },
    # Keep monthly partitions T+2 months ahead; alerts if any are missing (RFC-002 §5.3).
    "crm-ensure-partitions": {
        "task": "crm.ensure_partitions",
        "schedule": crontab(hour="3", minute="0"),  # daily 03:00
        "options": {"queue": "housekeeping"},
    },
    # Same for the conversation_parts firehose (messaging owns its own partitioned table).
    "messaging-ensure-partitions": {
        "task": "messaging.ensure_partitions",
        "schedule": crontab(hour="3", minute="5"),  # daily 03:05
        "options": {"queue": "housekeeping"},
    },
    # Drop expired idempotency keys so the ledger stays small (RFC-002 §5.6).
    "messaging-purge-idempotency": {
        "task": "messaging.purge_idempotency_keys",
        "schedule": crontab(hour="3", minute="10"),  # daily 03:10
        "options": {"queue": "housekeeping"},
    },
}


@celery_app.on_after_configure.connect
def _setup_logging(**_kwargs: object) -> None:
    configure_logging()
