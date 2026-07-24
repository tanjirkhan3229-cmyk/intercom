"""Shared test fixtures.

Integration tests run against a real Postgres (pgvector) via testcontainers, with the same
extensions + least-privilege roles as dev/prod and the **actual Alembic migrations** applied
as the ``migrator`` role. The app then runs as ``app_rw`` (RLS forced), so tests exercise the
true tenancy path. Unit tests need none of this.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

API_DIR = Path(__file__).resolve().parents[1]

_SUPERUSER = "postgres"
_SUPERPASS = "postgres"


def _bootstrap_roles_and_extensions(host: str, port: int) -> None:
    import psycopg

    with psycopg.connect(
        host=host, port=port, user=_SUPERUSER, password=_SUPERPASS, dbname="relay", autocommit=True
    ) as conn:
        cur = conn.cursor()
        for ext in ("citext", "vector", "pg_trgm", "btree_gin", "pgcrypto"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='migrator') THEN "
            "  CREATE ROLE migrator LOGIN PASSWORD 'migrator' BYPASSRLS; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_rw') THEN "
            "  CREATE ROLE app_rw LOGIN PASSWORD 'app_rw'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_ro') THEN "
            "  CREATE ROLE app_ro LOGIN PASSWORD 'app_ro'; END IF; "
            "END $$;"
        )
        cur.execute("GRANT ALL ON SCHEMA public TO migrator")
        cur.execute("ALTER SCHEMA public OWNER TO migrator")
        cur.execute("GRANT USAGE ON SCHEMA public TO app_rw, app_ro")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
            "GRANT SELECT ON TABLES TO app_ro"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO app_rw, app_ro"
        )


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def _database() -> Iterator[None]:
    """Boot Postgres + Redis, set DSNs, bootstrap roles/extensions, run migrations.

    Session-scoped. Redis is needed by the CRM event firehose (async buffer + sync drain).
    """
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    with (
        PostgresContainer(
            "pgvector/pgvector:pg16",
            username=_SUPERUSER,
            password=_SUPERPASS,
            dbname="relay",
            driver="asyncpg",
        ) as pg,
        RedisContainer("redis:7-alpine") as rc,
    ):
        host = pg.get_container_host_ip()
        port = int(pg.get_exposed_port(5432))
        redis_host = rc.get_container_host_ip()
        redis_port = int(rc.get_exposed_port(6379))

        os.environ["DATABASE_URL"] = f"postgresql+asyncpg://app_rw:app_rw@{host}:{port}/relay"
        os.environ["DATABASE_URL_RO"] = f"postgresql+asyncpg://app_ro:app_ro@{host}:{port}/relay"
        os.environ["MIGRATION_DATABASE_URL"] = (
            f"postgresql+psycopg://migrator:migrator@{host}:{port}/relay"
        )
        os.environ["REDIS_CACHE_URL"] = f"redis://{redis_host}:{redis_port}/0"
        os.environ["REDIS_BROKER_URL"] = f"redis://{redis_host}:{redis_port}/1"
        os.environ["JWT_SIGNING_KEY"] = "test-signing-key-at-least-32-bytes-long!!"
        os.environ["SECRET_ENCRYPTION_KEY"] = "test-encryption-key-at-least-32-bytes!!"
        os.environ["ENVIRONMENT"] = "test"

        from relay.settings import get_settings

        get_settings.cache_clear()

        _bootstrap_roles_and_extensions(host, port)
        _run_migrations()
        yield


@pytest.fixture(autouse=True)
async def _fresh_engines(_database: None) -> AsyncIterator[None]:
    """Rebind the app's async engines + Redis clients to the current test, then dispose.

    Also flushes the Redis cache DB and clears the global (non-RLS) ``outbox`` table so event
    buffers / undrained events never leak between tests — the outbox is shared infra
    (no ``workspace_id``), so a prior test's undrained rows would otherwise pollute a later one
    (e.g. the relay's global ``_fetch_pending``, or per-aggregate seq allocation).
    """
    import psycopg

    import relay.core.db as db
    import relay.core.redis as rds
    from relay.settings import get_settings

    db._engine = None
    db._engine_ro = None
    db._sessionmaker = None
    rds._async_client = None
    if rds._sync_client is not None:
        rds._sync_client.close()
        rds._sync_client = None
    rds.get_redis_sync().flushdb()
    with psycopg.connect(get_settings().database_url_psycopg, autocommit=True) as _conn:
        _conn.execute("DELETE FROM outbox")

    yield

    for engine in (db._engine, db._engine_ro):
        if engine is not None:
            await engine.dispose()
    db._engine = None
    db._engine_ro = None
    db._sessionmaker = None
    await rds.reset_async_redis()
    if rds._sync_client is not None:
        rds._sync_client.close()
        rds._sync_client = None


@pytest.fixture
def app_instance():
    from relay.main import create_app

    return create_app()


@pytest.fixture
async def client(app_instance) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
