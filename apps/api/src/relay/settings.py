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
