"""Celery tasks for the ``webhooks`` module (P0.11).

- ``webhooks.deliver``          (queue ``webhooks``) — sign + POST one delivery through the
  SSRF-guarded client; update the ledger + per-subscription breaker/auto-disable. Retries are
  **durable** (a ``next_attempt_at`` on the row + the ``scan_retries`` beat), never Celery ETA —
  a 72h ETA would pin a message in the broker for three days.
- ``webhooks.scan_retries``     (queue ``housekeeping``) — claim due deliveries across workspaces
  (SECURITY DEFINER visibility-timeout claim) and re-enqueue ``deliver``.
- ``webhooks.purge_deliveries`` (queue ``housekeeping``) — row-level DELETE of deliveries past the
  30-day retention window (``relay_purge_webhook_deliveries``; de-partitioned in 0018).

Tasks are synchronous (Celery workers run sync): raw ``psycopg`` + sync Redis, each idempotent.
The read (under RLS) commits before the HTTP call so no DB transaction is held during the ≤10s
POST; a second write transaction records the outcome.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from relay.core.breaker import RedisCircuitBreaker
from relay.core.crypto import InvalidToken, decrypt_secret
from relay.core.ids import IdPrefix, encode_public_id
from relay.core.logging import get_logger
from relay.core.redis import get_redis_sync
from relay.core.ssrf import SsrfError, guarded_post
from relay.settings import get_settings
from relay.worker import celery_app

from . import events, signing

log = get_logger(__name__)

_SCAN_BATCH = 200
# Visibility timeout stamped on a claimed ('delivering') row. The scan re-finds it only after this
# lapses, so a worker that crashes mid-attempt is recovered (must exceed the 10s HTTP timeout).
_CLAIM_LEASE_SECONDS = 120
_BACKOFF_BASE_SECONDS = 10.0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _backoff_delay(attempt: int, cap_hours: int) -> float:
    """Exponential backoff with full jitter, capped at the retry-window ceiling."""
    cap = cap_hours * 3600
    ceiling = min(cap, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    return secrets.SystemRandom().uniform(0, ceiling)


def _set_ws(cur: Any, workspace_id: str) -> None:
    cur.execute("SELECT set_config('app.ws', %s, true)", (workspace_id,))


def _build_signed_request(row: dict[str, Any], event_time: str) -> tuple[bytes, dict[str, str]]:
    """Build the canonical JSON body + signed headers for one delivery."""
    event_id = encode_public_id(IdPrefix.WEBHOOK_EVENT, row["outbox_id"])
    envelope = {
        "id": event_id,
        "topic": row["topic"],
        "created_at": event_time,
        "data": row["payload"],
    }
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = decrypt_secret(row["secret_ciphertext"])
    timestamp = int(_now().timestamp())
    headers = {
        "Content-Type": "application/json",
        signing.SIGNATURE_HEADER: signing.compute_signature(secret, timestamp, body),
        signing.TIMESTAMP_HEADER: str(timestamp),
        "Relay-Event-Id": event_id,
        "Relay-Topic": row["topic"],
        "User-Agent": "Relay-Webhooks/1.0",
    }
    return body, headers


@celery_app.task(name="webhooks.deliver", queue="webhooks", acks_late=True, max_retries=None)
def deliver(workspace_id: str, delivery_id: str, created_at: str) -> str:
    """Attempt one webhook delivery. Idempotent + never raises (durable retry via the ledger).

    This task is the SOLE claim point: ``_claim`` atomically transitions the row to 'delivering'
    and stamps a visibility-timeout lease on ``next_attempt_at``. That is authoritative for a
    directly-enqueued task, a scan-enqueued retry, AND a redelivered Celery message — only one wins
    (a row already 'delivering' with a live lease, or terminal, is not claimable). The retry scan
    only *finds* due rows and enqueues this task; it does not pre-claim (so it cannot lock the
    delivery out). A worker that crashes mid-attempt leaves the row 'delivering'; once the lease
    lapses the scan re-enqueues it and this task reclaims it.
    """
    settings = get_settings()
    redis = get_redis_sync()
    with psycopg.connect(settings.database_url_psycopg, row_factory=dict_row) as conn:
        conn.autocommit = False

        claimed = _claim(conn, workspace_id, delivery_id, created_at)
        if claimed is None:
            # Terminal, or a live claim held by another worker — expected under at-least-once
            # enqueue; logged (not silent) so a genuinely stuck claim is observable.
            log.info("webhooks.deliver.not_claimable", delivery_id=delivery_id)
            return "not_claimable"

        sub = _load_subscription(conn, workspace_id, claimed["subscription_id"])
        if sub is None:
            _finalize_terminal(conn, workspace_id, delivery_id, created_at, "subscription deleted")
            return "subscription_deleted"
        if sub["status"] == "disabled":
            _finalize_terminal(conn, workspace_id, delivery_id, created_at, "subscription disabled")
            return "subscription_disabled"

        breaker = RedisCircuitBreaker(
            redis,
            str(claimed["subscription_id"]),
            threshold=settings.webhook_breaker_threshold,
            cooldown_seconds=settings.webhook_breaker_cooldown_seconds,
        )
        attempt = claimed["attempt"] + 1
        if breaker.is_open():
            nxt = _now() + dt.timedelta(seconds=settings.webhook_breaker_cooldown_seconds)
            _record_skip(conn, workspace_id, delivery_id, created_at, attempt, nxt)
            return "breaker_open"

        # --- deliver (no DB transaction held during the ≤10s POST) ---
        row = {
            "outbox_id": claimed["outbox_id"],
            "topic": claimed["topic"],
            "payload": claimed["payload"],
            "secret_ciphertext": sub["secret_ciphertext"],
        }
        code: int | None = None
        try:
            # Inside the try: decrypt/sign can raise (e.g. a rotated encryption key → InvalidToken)
            # and MUST be treated as a delivery failure, never an unhandled crash (see below).
            body, headers = _build_signed_request(row, created_at)
            resp = guarded_post(
                sub["url"],
                content=body,
                headers=headers,
                timeout=settings.webhook_delivery_timeout_seconds,
                allow_private=settings.webhook_allow_private_targets,
            )
            code = resp.status_code
            ok = 200 <= code < 300
            error = None if ok else f"HTTP {code}"
        except SsrfError as exc:
            ok, error = False, f"ssrf: {exc.message}"
        except httpx.HTTPError as exc:
            ok, error = False, f"transport: {type(exc).__name__}"
        except InvalidToken:
            ok, error = False, "sign: signing secret not decryptable (encryption key rotated?)"
        except Exception as exc:  # never-raise contract: any unexpected failure is a delivery fail
            ok, error = False, f"error: {type(exc).__name__}"
            log.error("webhooks.deliver.unexpected", delivery_id=delivery_id, error=str(exc))

        # --- record outcome (second short transaction) ---
        if ok:
            breaker.record_success()
            _record_success(
                conn,
                workspace_id,
                delivery_id,
                created_at,
                attempt,
                code,
                claimed["subscription_id"],
            )
            return "delivered"

        breaker.record_failure()
        _record_failure(
            conn,
            workspace_id,
            delivery_id,
            created_at,
            attempt,
            code,
            error or "unknown",
            claimed["subscription_id"],
            sub["url"],
            settings.webhook_max_retry_hours,
            settings.webhook_auto_disable_failures,
        )
        return "failed"


def _claim(conn: Any, ws: str, delivery_id: str, created_at: str) -> dict[str, Any] | None:
    """Atomically claim a due delivery (visibility timeout). Returns its fields, or None if it is
    terminal or already held by another worker (next_attempt_at in the future). The row-level lock
    serialises concurrent claims: the loser re-evaluates the WHERE against the pushed
    next_attempt_at and matches nothing."""
    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            """UPDATE webhook_deliveries
               SET status='delivering', next_attempt_at = now() + make_interval(secs => %s)
               WHERE created_at=%s::timestamptz AND id=%s
                 AND status IN ('pending','failed','skipped_breaker_open','delivering')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= now())
               RETURNING attempt, outbox_id, topic, payload, subscription_id""",
            (_CLAIM_LEASE_SECONDS, created_at, delivery_id),
        )
        row: dict[str, Any] | None = cur.fetchone()
    conn.commit()
    return row


def _load_subscription(conn: Any, ws: str, sub_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            "SELECT url, secret_ciphertext, status FROM webhook_subscriptions WHERE id=%s",
            (sub_id,),
        )
        row: dict[str, Any] | None = cur.fetchone()
    conn.commit()
    return row


def _record_success(
    conn: Any,
    ws: str,
    delivery_id: str,
    created_at: str,
    attempt: int,
    code: int | None,
    sub_id: uuid.UUID,
) -> None:
    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            """UPDATE webhook_deliveries
               SET status='delivered', delivered_at=now(), response_code=%s, error=NULL,
                   attempt=%s, next_attempt_at=NULL
               WHERE created_at=%s::timestamptz AND id=%s""",
            (code, attempt, created_at, delivery_id),
        )
        cur.execute(
            """UPDATE webhook_subscriptions
               SET consecutive_failures=0, last_success_at=now(), last_error=NULL
               WHERE id=%s""",
            (sub_id,),
        )
    conn.commit()


def _record_skip(
    conn: Any, ws: str, delivery_id: str, created_at: str, attempt: int, nxt: dt.datetime
) -> None:
    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            """UPDATE webhook_deliveries
               SET status='skipped_breaker_open', attempt=%s, next_attempt_at=%s
               WHERE created_at=%s::timestamptz AND id=%s""",
            (attempt, nxt, created_at, delivery_id),
        )
    conn.commit()


def _finalize_terminal(conn: Any, ws: str, delivery_id: str, created_at: str, error: str) -> None:
    """Mark a delivery permanently undeliverable (subscription deleted/disabled). Uses the terminal
    'exhausted' status (NOT the retryable 'failed') so a redelivered task cannot re-claim it."""
    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            """UPDATE webhook_deliveries
               SET status='exhausted', error=%s, next_attempt_at=NULL
               WHERE created_at=%s::timestamptz AND id=%s""",
            (error, created_at, delivery_id),
        )
    conn.commit()


def _record_failure(
    conn: Any,
    ws: str,
    delivery_id: str,
    created_at: str,
    attempt: int,
    code: int | None,
    error: str,
    sub_id: uuid.UUID,
    url: str,
    max_retry_hours: int,
    auto_disable_failures: int,
) -> None:
    delivery_created = dt.datetime.fromisoformat(created_at)
    exhausted = (_now() - delivery_created) > dt.timedelta(hours=max_retry_hours)
    if exhausted:
        status, nxt = "exhausted", None
    else:
        status = "failed"
        nxt = _now() + dt.timedelta(seconds=_backoff_delay(attempt, max_retry_hours))

    with conn.cursor() as cur:
        _set_ws(cur, ws)
        cur.execute(
            """UPDATE webhook_deliveries
               SET status=%s, response_code=%s, error=%s, attempt=%s, next_attempt_at=%s
               WHERE created_at=%s::timestamptz AND id=%s""",
            (status, code, error, attempt, nxt, created_at, delivery_id),
        )
        cur.execute(
            """UPDATE webhook_subscriptions
               SET consecutive_failures = consecutive_failures + 1, last_error=%s
               WHERE id=%s
               RETURNING consecutive_failures""",
            (error, sub_id),
        )
        result = cur.fetchone()
        failures = int(result["consecutive_failures"]) if result else 0

        # Auto-disable after sustained failure; emit a notify event in the SAME txn (master rule 2).
        if failures >= auto_disable_failures:
            cur.execute(
                """UPDATE webhook_subscriptions
                   SET status='disabled', disabled_at=now()
                   WHERE id=%s AND status <> 'disabled'""",
                (sub_id,),
            )
            if cur.rowcount > 0:
                _emit_disabled_event(cur, ws, sub_id, url, error)
                log.error("webhooks.subscription.auto_disabled", subscription_id=str(sub_id))
    conn.commit()


def _emit_disabled_event(cur: Any, ws: str, sub_id: uuid.UUID, url: str, last_error: str) -> None:
    """Append a ``webhook.subscription.disabled`` outbox row (raw SQL replicating outbox.emit)."""
    payload = {
        "workspace_id": encode_public_id(IdPrefix.WORKSPACE, uuid.UUID(ws)),
        "subscription_id": encode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_id),
        "url": url,
        "last_error": last_error,
    }
    cur.execute(
        """INSERT INTO outbox (id, aggregate, aggregate_id, seq, topic, payload, created_at)
           VALUES (%s, %s, %s,
                   (SELECT COALESCE(MAX(seq), 0) + 1 FROM outbox WHERE aggregate_id = %s),
                   %s, %s::jsonb, now())""",
        (
            uuid.uuid4(),
            events.AGGREGATE_WEBHOOK_SUBSCRIPTION,
            sub_id,
            sub_id,
            events.SUBSCRIPTION_DISABLED,
            json.dumps(payload),
        ),
    )
    cur.execute("NOTIFY relay_outbox")


@celery_app.task(name="webhooks.scan_retries", queue="housekeeping")
def scan_retries() -> int:
    """Find due deliveries (retries, manual redeliveries, crash-recovered rows) and enqueue
    ``deliver``. It only *finds* — the ``deliver`` task does the authoritative atomic claim — so the
    scan can never lock a delivery out (the bug of pre-hiding the row before hand-off). Re-enqueuing
    a row a delivering worker already holds is harmless: that worker's task fails the claim and
    returns not_claimable."""
    with (
        psycopg.connect(get_settings().database_url_psycopg, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT workspace_id, id, created_at FROM relay_due_webhook_deliveries(%s)",
            (_SCAN_BATCH,),
        )
        rows = cur.fetchall()
    for ws, delivery_id, created in rows:
        celery_app.send_task(
            "webhooks.deliver",
            args=[str(ws), str(delivery_id), created.isoformat()],
            queue="webhooks",
        )
    if rows:
        log.info("webhooks.scan_retries.enqueued", count=len(rows))
    return len(rows)


@celery_app.task(name="webhooks.purge_deliveries", queue="housekeeping")
def purge_deliveries(keep_days: int = 30) -> int:
    """Delete ``webhook_deliveries`` rows older than ``keep_days`` (30-day retention).

    Row-level ``DELETE`` via the ``relay_purge_webhook_deliveries`` SECURITY DEFINER function
    (BYPASSRLS owner → workspace-agnostic sweep; EXECUTE-granted to ``app_rw``). Replaced the
    drop-old-partition path when the table was de-partitioned (0018)."""
    with (
        psycopg.connect(get_settings().database_url_psycopg, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT relay_purge_webhook_deliveries(%s)", (keep_days,))
        result = cur.fetchone()
    deleted = int(result[0]) if result else 0
    if deleted:
        log.info("webhooks.purge.deleted_rows", deleted=deleted)
    return deleted
