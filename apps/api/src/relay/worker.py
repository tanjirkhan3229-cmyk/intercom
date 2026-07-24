"""Celery application (RFC-001 §6.4 — the segregated `workers` runtime shape).

Queues are bulkheads: bursty/slow work must never starve the interactive path. Every
task is idempotent (at-least-once delivery); DLQ + bounded retries are the default.
Actual tasks are registered by feature modules as they are built.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_init
from kombu import Queue

from relay.core.logging import configure_logging
from relay.core.observability import init_worker_observability, start_metrics_server
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
    include=[
        "relay.modules.crm.tasks",
        "relay.modules.messaging.tasks",
        "relay.modules.billing.tasks",
        "relay.modules.channels.tasks",
        "relay.modules.reporting.tasks",
        "relay.modules.webhooks.tasks",
        "relay.modules.knowledge.tasks",
        "relay.modules.ai.tasks",
    ],
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
    # Seat counting (RFC-002 §5.6, P0.10): daily full reconciliation + a tight poll that
    # pushes only dirty rows to Stripe, so an on-change seat add reflects within minutes
    # without ever calling Stripe from the request path.
    "billing-recalculate-all-seats": {
        "task": "billing.recalculate_all_seats",
        "schedule": crontab(hour="3", minute="15"),  # daily 03:15
        "options": {"queue": "housekeeping"},
    },
    "billing-sync-seats-to-stripe": {
        "task": "billing.sync_seats_to_stripe",
        "schedule": 300.0,  # every 5 minutes
        "options": {"queue": "housekeeping"},
    },
    # Neko resolution metering (P1.3, RFC-003 §8-9). Same dirty-poll shape as seats: push only
    # un-synced ``usage_records`` to Stripe Billing Meters (never from the request path), plus a
    # monthly reconciliation that logs the authoritative total and re-pushes anything left behind.
    "billing-sync-resolutions-to-stripe": {
        "task": "billing.sync_resolutions_to_stripe",
        "schedule": 300.0,  # every 5 minutes
        "options": {"queue": "housekeeping"},
    },
    "billing-reconcile-usage-monthly": {
        "task": "billing.reconcile_usage_monthly",
        "schedule": crontab(day_of_month="1", hour="4", minute="0"),  # 1st of month, 04:00
        "options": {"queue": "housekeeping"},
    },
    # 72 h-silence resolutions (RFC-003 §8): meter conversations Neko answered that the customer
    # left silent. A conversation crossing the window is metered within one sweep.
    "ai-scan-silence-resolutions": {
        "task": "ai.scan_silence_resolutions",
        "schedule": crontab(minute="*/15"),  # every 15 minutes
        "options": {"queue": "housekeeping"},
    },
    # Verify pending sending domains (P0.7 email — DNS/SES check).
    "channels-poll-domains": {
        "task": "channels.poll_domains",
        "schedule": 300.0,  # every 5 minutes
        "options": {"queue": "housekeeping"},
    },
    # Recompute reporting daily rollups (today + yesterday) hourly, so the volume/CSAT reports
    # track through the day and catch late closes/ratings (P0.9, RFC-000 §2.9). Idempotent.
    "reporting-daily-rollups": {
        "task": "reporting.compute_daily_rollups",
        "schedule": crontab(minute="0"),  # top of every hour
        "options": {"queue": "analytics"},
    },
    # Re-enqueue due webhook retries + manual redeliveries (P0.11 durable retry ledger).
    "webhooks-scan-retries": {
        "task": "webhooks.scan_retries",
        "schedule": 30.0,  # seconds
        "options": {"queue": "housekeeping"},
    },
    # Keep webhook_deliveries partitions ahead + drop those past the 30-day retention window.
    "webhooks-ensure-partitions": {
        "task": "webhooks.ensure_partitions",
        "schedule": crontab(hour="3", minute="20"),  # daily 03:20
        "options": {"queue": "housekeeping"},
    },
    "webhooks-purge-deliveries": {
        "task": "webhooks.purge_deliveries",
        "schedule": crontab(hour="3", minute="25"),  # daily 03:25
        "options": {"queue": "housekeeping"},
    },
}


@celery_app.on_after_configure.connect
def _setup_logging(**_kwargs: object) -> None:
    configure_logging()
    # Sentry + OTel (Celery instrumentation) + task-metric signals — all no-ops unless configured.
    init_worker_observability()


@worker_init.connect
def _start_worker_metrics(**_kwargs: object) -> None:
    # Fires once in the parent worker process (before prefork), so a single scrape server binds
    # ``metrics_port``. In multiprocess mode children write to PROMETHEUS_MULTIPROC_DIR.
    start_metrics_server()
