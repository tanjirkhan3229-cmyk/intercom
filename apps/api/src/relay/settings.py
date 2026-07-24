"""Typed application settings, loaded from environment / .env (RFC-001 §13: no env-baked secrets).

Import the singleton via ``get_settings()`` so it is constructed once and cached.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "development"
    log_level: str = "INFO"

    # --- Database (RFC-002 §7, §9) ---
    # Async runtime uses asyncpg; Alembic uses the sync psycopg DSN.
    database_url: str = "postgresql+asyncpg://app_rw:app_rw_dev@localhost:5432/relay"
    database_url_ro: str = "postgresql+asyncpg://app_ro:app_ro_dev@localhost:5432/relay"
    migration_database_url: str = "postgresql+psycopg://migrator:migrator_dev@localhost:5432/relay"

    # --- Redis (cache/pubsub + celery broker) ---
    redis_cache_url: str = "redis://localhost:6379/0"
    redis_broker_url: str = "redis://localhost:6380/0"

    # --- Object storage ---
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "relay"
    s3_secret_access_key: str = "relay_dev_secret"
    s3_bucket_attachments: str = "relay-attachments"
    s3_bucket_exports: str = "relay-exports"

    # --- SMTP sink ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025

    # --- Email channel (P0.7, RFC-001 §6.6/§9) ---
    # Outbound transport is chosen by ``email_transport``: ``smtp`` (dev → Mailpit), ``ses``
    # (staging/prod), or ``memory`` (tests capture via the FakeSender, no network). SES uses the
    # same MinIO/S3 boto credentials pattern (storage._client); ``ses_endpoint_url`` allows a
    # local/staging override. Inbound arrives as SES-receipt → S3 raw MIME → SNS → the /inbound
    # webhook → the ``ingest`` queue.
    email_transport: Literal["smtp", "ses", "memory"] = "smtp"
    ses_region: str = "us-east-1"
    ses_endpoint_url: str | None = None
    ses_access_key_id: str | None = None  # None → boto default chain (IAM role)
    ses_secret_access_key: str | None = None
    ses_configuration_set: str | None = None  # bounce/complaint event publishing
    # The workspace-agnostic inbound domain that carries plus-addressed reply tokens
    # (``reply+{token}@{email_inbound_domain}``). Per-workspace sending domains live in the
    # ``verified_domains`` table.
    email_inbound_domain: str = "inbound.relay.dev"
    email_from_name: str = "Relay"
    # Dedicated HMAC key for stateless reply tokens — deliberately separate from
    # ``jwt_signing_key`` so the two token audiences never cross (mirrors centrifugo_token_secret).
    email_reply_token_secret: str = Field(
        default="dev-email-reply-token-secret-change-me", min_length=16
    )
    # Max raw MIME size we will accept/emit (SES receive/send limits are ~40 MB). Outbound over
    # this is rejected at the service layer; inbound over this drops the attachment + notes it.
    email_max_message_bytes: int = 40 * 1024 * 1024
    # Global outbound send-rate cap (token bucket, RFC-001 §9 bounce-storm guard). None = no cap
    # (dev/tests). Per-tenant caps graduate with campaigns (P1.8). Prod sets this to the SES rate.
    email_send_rate_per_sec: int | None = None
    # Bucket holding SES-written raw inbound MIME (SES receipt rule action target).
    s3_bucket_email_inbound: str = "relay-email-inbound"
    # SNS webhook signature verification. Disable ONLY in tests (the verifier is exercised by its
    # own unit test with a captured payload); staging/prod keep it on.
    sns_verify_signatures: bool = True

    # --- Security (RFC-001 §10) ---
    jwt_signing_key: str = Field(default="dev-only-change-me-please-32-bytes-min", min_length=16)
    secret_encryption_key: str = Field(
        default="dev-only-change-me-please-32-bytes-min", min_length=16
    )
    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # Widget contact/lead session (RFC-001 §10): long-lived so a lead's cookie survives visits,
    # but low-privilege (own conversations only). Rotation/refresh can tighten this later.
    widget_session_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days

    # --- Google OIDC (optional) ---
    google_oidc_client_id: str | None = None
    google_oidc_client_secret: str | None = None
    google_oidc_redirect_uri: str | None = None

    # --- Billing / Stripe (RFC-002 §5.6, P0.10) ---
    stripe_api_base: str = "https://api.stripe.com"
    # Pin the Stripe API version so provider-side upgrades never silently change payload
    # shapes under us (RFC-001 §5 provider discipline). Bump deliberately, with a test run.
    stripe_api_version: str = "2024-06-20"
    stripe_secret_key: str = "sk_test_dev_placeholder"
    stripe_webhook_secret: str = "whsec_dev_placeholder"
    stripe_checkout_success_url: str = "http://localhost:3000/billing/success"
    stripe_checkout_cancel_url: str = "http://localhost:3000/billing/cancel"
    stripe_portal_return_url: str = "http://localhost:3000/settings/billing"
    billing_trial_days: int = 14

    # --- Realtime gateway (Centrifugo — RFC-001 §6.1 gateway row, §6.3) ---
    # Realtime is bought, not built: the API mints per-connection + per-channel HS256 tokens and
    # publishes fan-out via Centrifugo's server API. ``token_secret`` must match Centrifugo's
    # ``token_hmac_secret_key`` (see infra/centrifugo/config.json); it is deliberately *separate*
    # from ``jwt_signing_key`` so the two token audiences never cross.
    centrifugo_api_url: str = (
        "http://localhost:8001"  # server→Centrifugo (compose: http://centrifugo:8000)
    )
    centrifugo_ws_url: str = "ws://localhost:8001/connection/websocket"  # client-facing
    centrifugo_api_key: str = Field(default="dev-centrifugo-api-key", min_length=8)
    centrifugo_token_secret: str = Field(
        default="dev-centrifugo-token-secret-change-me", min_length=16
    )
    centrifugo_token_ttl_seconds: int = 60 * 30  # connection/subscription token lifetime

    # realtime_fallback kill switch (RFC-001 §6.3): when on, clients may downgrade to long-poll.
    # ponytail: a settings bool, not an Unleash client — one flag doesn't justify the dependency.
    # Per-workspace override + true runtime toggling arrive with the flag service (P1).
    realtime_fallback: bool = True

    # --- Help Center (P0.8, RFC-001 §6.1 `web` ISR row) ---
    # The hosted Help Center runs on Next.js ISR. On publish/unpublish the knowledge module
    # writes an outbox event; the ``help-center-revalidate`` consumer POSTs the affected paths
    # to ``help_center_revalidate_url`` (the site's /api/revalidate route) so the ISR cache
    # refreshes within seconds. Leave the URL unset to rely on time-based ISR only.
    help_center_base_url: str = "http://localhost:3000"
    help_center_revalidate_url: str | None = None
    help_center_revalidate_secret: str = Field(
        default="dev-help-center-revalidate-secret", min_length=8
    )

    # --- Public API (P0.11, RFC-001 §10) ---
    # Per-workspace token bucket applied ONLY to API-key traffic (the first-party agent app uses
    # JWTs and is never rate-limited here). ``capacity`` = burst; ``refill`` = tokens/sec. Tests
    # set tiny values to force a 429.
    public_api_rate_limit_enabled: bool = True
    public_api_rate_capacity: int = 120
    public_api_rate_refill_per_sec: float = 2.0

    # --- Webhooks (P0.11, RFC-001 §6.7, §10) ---
    webhook_delivery_timeout_seconds: float = 10.0
    webhook_max_retry_hours: int = 72
    webhook_breaker_threshold: int = 5
    webhook_breaker_cooldown_seconds: int = 60
    webhook_auto_disable_failures: int = 20
    webhook_signature_tolerance_seconds: int = 300
    # SSRF egress guard: when False (prod), webhook URLs must be https and must not resolve to
    # private/loopback/link-local/metadata IPs. Tests/dev set True to allow a localhost receiver.
    webhook_allow_private_targets: bool = False

    # --- Workflows / automation (P1.5, RFC-001 §6.7) ---
    # Master kill switch: when off, the trigger consumer + executor tasks no-op (risky work behind a
    # flag — DoD). ``workflow_sla_action_enabled`` gates the apply_sla action, which lands in P1.7.
    workflows_enabled: bool = True
    workflow_sla_action_enabled: bool = False
    # Runaway backstop: max nodes advanced in one run. Termination is actually guaranteed by the
    # acyclic-graph validation (each node runs ≤ once), so this only needs to sit safely ABOVE the
    # graph node cap (graph._MAX_NODES = 200) — never trip a valid max-size linear graph.
    workflow_run_step_budget: int = 1000
    # Durable-timer claim lease + reaper staleness window (seconds). The reaper re-drives running/
    # suspended runs idle longer than this — the broker-flush recovery path.
    workflow_timer_lease_seconds: int = 120
    workflow_run_stale_seconds: int = 120
    # call_webhook action: bound + retry the external POST (SSRF-guarded like webhook delivery).
    # ``workflow_action_max_retries`` bounds real HTTP attempts (retries are driven by the reaper);
    # ``workflow_action_lease_seconds`` is the per-attempt lease that blocks a concurrent/duplicate
    # POST for the same node — it MUST exceed the action timeout and be < the reaper stale window
    # (so a genuinely stuck attempt is retried, but an in-flight one is never double-fired).
    workflow_action_timeout_seconds: float = 10.0
    workflow_action_max_retries: int = 5
    workflow_action_lease_seconds: int = 30
    workflow_breaker_threshold: int = 5
    workflow_breaker_cooldown_seconds: int = 60

    # --- Observability (P0.12, RFC-001 §9/§13) ---
    # Prometheus: the `app` serves /metrics; the non-HTTP shapes (worker/beat/relay/fanout) start a
    # scrape server on ``metrics_port``. In prod set ``PROMETHEUS_MULTIPROC_DIR`` so the
    # prefork/uvicorn children share one series set; unset → single-process registry (dev/tests).
    metrics_enabled: bool = True
    metrics_port: int = 9100
    # OpenTelemetry tracing is OFF unless an OTLP endpoint is configured — a no-op in dev/tests, so
    # no collector is required. When on, traces correlate request → outbox → worker (RFC-001 §6.5).
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "relay"
    otel_traces_sampler_ratio: float = 1.0
    # Sentry error tracking — OFF unless a DSN is set. ``deploy_sha`` doubles as the Sentry release
    # and the Prometheus build-info / deploy-marker label (RFC-001 §13 canary deploy markers).
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    deploy_sha: str = "unknown"

    @property
    def otel_enabled(self) -> bool:
        return bool(self.otel_exporter_otlp_endpoint)

    @property
    def database_url_psycopg(self) -> str:
        """Plain (driverless) DSN for raw psycopg use in sync Celery tasks (e.g. the events
        COPY drain). Derived from the async ``database_url`` by dropping the ``+asyncpg`` tag
        so ``psycopg.connect`` accepts it — same ``app_rw`` credentials, RLS still forced."""
        return self.database_url.replace("+asyncpg", "").replace("+psycopg", "")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def google_oidc_enabled(self) -> bool:
        return bool(self.google_oidc_client_id and self.google_oidc_client_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
