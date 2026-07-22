"""Integration smoke test: the CI stack (Postgres+pgvector, Redis) is reachable.

Uses testcontainers so CI needs no pre-provisioned services (RFC-001 §13). Richer
tenancy/RLS integration tests build on the fixtures added in P0.1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def test_postgres_with_pgvector_boots() -> None:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(), future=True)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
            # Extensions Relay depends on must be installable in the CI image.
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
            conn.commit()
            ext = conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
            assert ext == "vector"
        engine.dispose()


def test_redis_boots() -> None:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis_c:
        client = redis_c.get_client(decode_responses=True)
        assert client.ping() is True
        client.set("relay:smoke", "ok")
        assert client.get("relay:smoke") == "ok"
