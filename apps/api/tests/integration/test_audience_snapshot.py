"""P1.8 audience snapshot — the compiled SQL matches ``core.predicates.evaluate`` over real rows.

Seeds a spread of contacts (core fields + typed custom attributes), then for a battery of
predicates asserts that ``crm.service.snapshot_audience`` (predicate→SQL on ``app_ro``,
keyset-paged) returns exactly the set the Python evaluator selects over the same contacts. Also
proves soft-deleted contacts are excluded and that ``contains`` is LIKE-injection-safe.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from uuid import uuid4

import httpx
import pytest

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.predicates import evaluate
from relay.modules.crm import service as crm_service
from relay.modules.crm.models import AttributeDefinition, Contact

pytestmark = pytest.mark.integration

PASSWORD = "password123"

# (email, name, kind, phone, custom, deleted) — the seed spread.
_SEED: list[tuple[str, str, str, str | None, dict[str, Any], bool]] = [
    (
        "a@ex.com",
        "Alice",
        "user",
        "111",
        {"plan": "pro", "seats": 10, "vip": True, "tags": ["beta", "vip"]},
        False,
    ),
    (
        "b@ex.com",
        "Bob",
        "lead",
        None,
        {"plan": "free", "seats": 2, "vip": False, "tags": ["beta"]},
        False,
    ),
    ("c@ex.com", "Carol", "user", "333", {"plan": "pro", "seats": 5}, False),
    ("d@ex.com", "Dave", "user", None, {}, False),
    (
        "e%test@ex.com",
        "Eve",
        "user",
        None,
        {"plan": "enterprise", "seats": 50, "tags": ["vip"]},
        False,
    ),
    ("z@ex.com", "Zed", "user", None, {"plan": "pro"}, True),  # soft-deleted: never in an audience
]

_PREDICATES: list[dict[str, Any]] = [
    {},
    {"op": "eq", "field": "email", "value": "a@ex.com"},
    {"op": "ne", "field": "kind", "value": "lead"},
    {"op": "in", "field": "kind", "value": ["user"]},
    {"op": "eq", "field": "custom.plan", "value": "pro"},
    {"op": "gt", "field": "custom.seats", "value": 5},
    {"op": "gte", "field": "custom.seats", "value": 5},
    {"op": "eq", "field": "custom.vip", "value": True},
    {"op": "exists", "field": "custom.vip"},
    {"op": "not_exists", "field": "custom.tags"},
    {"op": "contains", "field": "custom.tags", "value": "vip"},
    {"op": "contains", "field": "name", "value": "l"},
    {"op": "not_exists", "field": "phone"},
    {
        "op": "and",
        "clauses": [
            {"op": "eq", "field": "custom.plan", "value": "pro"},
            {"op": "gt", "field": "custom.seats", "value": 6},
        ],
    },
    {
        "op": "or",
        "clauses": [
            {"op": "eq", "field": "kind", "value": "lead"},
            {"op": "eq", "field": "custom.plan", "value": "enterprise"},
        ],
    },
    {"op": "contains", "field": "email", "value": "%"},  # LIKE-injection: only the literal '%' row
]


async def _owner(client: httpx.AsyncClient, ws_name: str) -> uuid.UUID:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201
    return decode_public_id(IdPrefix.WORKSPACE, resp.json()["workspace"]["id"])


def _context(
    email: str, name: str, kind: str, phone: str | None, custom: dict[str, Any]
) -> dict[str, Any]:
    return {"email": email, "name": name, "kind": kind, "phone": phone, "custom": custom}


async def test_snapshot_matches_evaluate_over_real_rows(client: httpx.AsyncClient) -> None:
    ws = await _owner(client, "AudienceWS")

    # Register the custom attribute types the predicates reference, then seed contacts directly.
    id_to_ctx: dict[uuid.UUID, dict[str, Any]] = {}
    async with session_scope(ws) as s:
        for name, data_type in (
            ("plan", "string"),
            ("seats", "number"),
            ("vip", "boolean"),
            ("tags", "list"),
        ):
            s.add(
                AttributeDefinition(
                    workspace_id=ws, entity="contact", name=name, data_type=data_type
                )
            )
        for email, name, kind, phone, custom, deleted in _SEED:
            c = Contact(
                workspace_id=ws,
                kind=kind,
                email=email,
                name=name,
                phone=phone,
                custom=custom,
                deleted_at=dt.datetime.now(dt.UTC) if deleted else None,
            )
            s.add(c)
            await s.flush()
            if not deleted:
                id_to_ctx[c.id] = _context(email, name, kind, phone, custom)

    for predicate in _PREDICATES:
        expected = {
            cid for cid, ctx in id_to_ctx.items() if (not predicate or evaluate(predicate, ctx))
        }
        actual: set[uuid.UUID] = set()
        # batch_size=2 forces multiple keyset pages.
        async for batch in crm_service.snapshot_audience(ws, predicate, batch_size=2):
            actual.update(cid for cid, _email in batch)
        assert actual == expected, f"predicate {predicate} mismatch: {actual ^ expected}"


async def test_snapshot_excludes_soft_deleted(client: httpx.AsyncClient) -> None:
    ws = await _owner(client, "DelWS")
    async with session_scope(ws) as s:
        s.add(
            Contact(
                workspace_id=ws,
                kind="user",
                email="live@ex.com",
                deleted_at=None,
            )
        )
        s.add(
            Contact(
                workspace_id=ws,
                kind="user",
                email="gone@ex.com",
                deleted_at=dt.datetime.now(dt.UTC),
            )
        )

    emails: set[str | None] = set()
    async for batch in crm_service.snapshot_audience(ws, {}, batch_size=100):
        emails.update(email for _cid, email in batch)
    assert "live@ex.com" in emails
    assert "gone@ex.com" not in emails
