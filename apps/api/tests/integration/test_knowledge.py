"""Knowledge / Help Center integration tests (P0.8 acceptance, RFC-000 §2.5).

Covers the P0.8 acceptance list:
- publish → the article is live on the public API; the revalidation hook fires (outbox row
  with the affected paths) + the consumer forwards them;
- FTS ranks title matches above body matches (``websearch_to_tsquery`` + weighted ``search_tsv``);
- unpublished articles 404 on the public API but are visible to a logged-in admin (preview);
plus the master-rule-1 tenancy guarantees (cross-tenant isolation + the unset-GUC backstop).
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import get_sessionmaker, session_scope

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, str, str]:
    """Sign up an owner; return (access_token, workspace_public_id, workspace_slug)."""
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"], body["workspace"]["slug"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _para(text: str) -> dict[str, object]:
    return {"blocks": [{"type": "paragraph", "text": text}]}


async def _create_article(
    client: httpx.AsyncClient, tok: str, *, title: str, body: dict[str, object] | None = None
) -> dict:
    resp = await client.post(
        "/v0/articles",
        json={"title": title, "body": body or _para("Body text.")},
        headers=_auth(tok),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- publish / draft visibility (acceptance #3 + public exposure) -------------


async def test_publish_makes_article_public_and_draft_is_hidden(client: httpx.AsyncClient) -> None:
    tok, _ws, slug = await _owner(client, "Acme")
    article = await _create_article(client, tok, title="Welcome", body=_para("Hello there."))
    art_slug = article["slug"]

    # Draft: admin can preview it (logged in), public API 404s.
    admin_view = await client.get(f"/v0/articles/{article['id']}", headers=_auth(tok))
    assert admin_view.status_code == 200
    assert admin_view.json()["status"] == "draft"

    public_draft = await client.get(f"/v0/hc/{slug}/articles/{art_slug}")
    assert public_draft.status_code == 404  # unpublished ⇒ 404 publicly

    # Publish → now live on the public API.
    pub = await client.post(f"/v0/articles/{article['id']}/publish", headers=_auth(tok))
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published"
    assert pub.json()["published_at"] is not None

    public = await client.get(f"/v0/hc/{slug}/articles/{art_slug}")
    assert public.status_code == 200
    assert public.json()["title"] == "Welcome"

    # Unpublish → hidden again.
    await client.post(f"/v0/articles/{article['id']}/unpublish", headers=_auth(tok))
    assert (await client.get(f"/v0/hc/{slug}/articles/{art_slug}")).status_code == 404


# --- FTS rank: title above body (acceptance #2) -------------------------------


async def test_fts_ranks_title_matches_above_body_matches(client: httpx.AsyncClient) -> None:
    tok, _ws, slug = await _owner(client, "Search")

    # "refund" only in the TITLE of one article, only in the BODY of the other.
    title_hit = await _create_article(
        client, tok, title="Refund policy", body=_para("Our store terms and conditions.")
    )
    body_hit = await _create_article(
        client, tok, title="Getting started", body=_para("How to request a refund from support.")
    )
    for a in (title_hit, body_hit):
        pub = await client.post(f"/v0/articles/{a['id']}/publish", headers=_auth(tok))
        assert pub.status_code == 200

    resp = await client.get(f"/v0/hc/{slug}/search", params={"q": "refund"})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    slugs = [r["slug"] for r in results]
    assert title_hit["slug"] in slugs and body_hit["slug"] in slugs
    # Title match must rank first (weight A > weight B).
    assert results[0]["slug"] == title_hit["slug"]
    assert results[0]["rank"] >= results[1]["rank"]


async def test_search_excludes_unpublished(client: httpx.AsyncClient) -> None:
    tok, _ws, slug = await _owner(client, "SearchDraft")
    await _create_article(client, tok, title="Secret draft", body=_para("hidden knowledge"))
    resp = await client.get(f"/v0/hc/{slug}/search", params={"q": "hidden"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# --- collections + public help center listing ---------------------------------


async def test_collection_grouping_and_public_listing(client: httpx.AsyncClient) -> None:
    tok, _ws, slug = await _owner(client, "Grouped")
    coll = await client.post(
        "/v0/collections",
        json={"name": "Billing", "description": "Money matters"},
        headers=_auth(tok),
    )
    assert coll.status_code == 201, coll.text
    coll_id = coll.json()["id"]
    coll_slug = coll.json()["slug"]

    resp = await client.post(
        "/v0/articles",
        json={"title": "Invoices", "collection_id": coll_id, "body": _para("About invoices.")},
        headers=_auth(tok),
    )
    art = resp.json()
    await client.post(f"/v0/articles/{art['id']}/publish", headers=_auth(tok))

    # Public help center home lists the collection (it has ≥1 published article).
    home = await client.get(f"/v0/hc/{slug}")
    assert home.status_code == 200
    collections = home.json()["collections"]
    assert any(c["slug"] == coll_slug and c["article_count"] == 1 for c in collections)

    # Public collection page lists the published article.
    page = await client.get(f"/v0/hc/{slug}/collections/{coll_slug}")
    assert page.status_code == 200
    assert [a["slug"] for a in page.json()["articles"]] == [art["slug"]]


async def test_slug_autogenerated_and_deduped(client: httpx.AsyncClient) -> None:
    tok, _ws, _slug = await _owner(client, "Slugs")
    a1 = await _create_article(client, tok, title="Same Title")
    a2 = await _create_article(client, tok, title="Same Title")
    assert a1["slug"] == "same-title"
    assert a2["slug"] == "same-title-2"  # deduped, never collides


async def test_admin_update_paths_survive_the_update_flush(client: httpx.AsyncClient) -> None:
    """Regression: UPDATE paths must not leave ``updated_at`` expired (async refresh 500s).

    Covers collection update and a *second* help-center PATCH (the UPDATE branch, not the
    first-time INSERT), which read ``updated_at`` back into the response DTO.
    """
    tok, _ws, _slug = await _owner(client, "Updates")
    coll = (await client.post("/v0/collections", json={"name": "Docs"}, headers=_auth(tok))).json()
    upd_c = await client.patch(
        f"/v0/collections/{coll['id']}", json={"name": "Documentation"}, headers=_auth(tok)
    )
    assert upd_c.status_code == 200, upd_c.text
    assert upd_c.json()["name"] == "Documentation"

    await client.patch("/v0/help-center", json={"name": "First"}, headers=_auth(tok))  # INSERT
    upd_hc = await client.patch(
        "/v0/help-center", json={"name": "Second"}, headers=_auth(tok)
    )  # UPDATE
    assert upd_hc.status_code == 200, upd_hc.text
    assert upd_hc.json()["name"] == "Second"


# --- help center theming ------------------------------------------------------


async def test_help_center_theming_surfaces_publicly(client: httpx.AsyncClient) -> None:
    tok, _ws, slug = await _owner(client, "Themed")
    upd = await client.patch(
        "/v0/help-center",
        json={"name": "Themed Support", "primary_color": "#C2410C"},
        headers=_auth(tok),
    )
    assert upd.status_code == 200, upd.text
    home = await client.get(f"/v0/hc/{slug}")
    assert home.status_code == 200
    assert home.json()["name"] == "Themed Support"
    assert home.json()["primary_color"] == "#C2410C"


async def test_public_help_center_reachable_by_workspace_app_id(client: httpx.AsyncClient) -> None:
    """The embedded widget boots with app_id (wrk_…), not the slug — the public API accepts both."""
    tok, ws_id, _slug = await _owner(client, "WidgetHC")
    art = await _create_article(client, tok, title="Widget article")
    await client.post(f"/v0/articles/{art['id']}/publish", headers=_auth(tok))

    by_app_id = await client.get(f"/v0/hc/{ws_id}/articles/{art['slug']}")
    assert by_app_id.status_code == 200
    assert by_app_id.json()["title"] == "Widget article"


async def test_unknown_help_center_slug_404s(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v0/hc/no-such-workspace")).status_code == 404
    search = await client.get("/v0/hc/no-such-workspace/search", params={"q": "x"})
    assert search.status_code == 404


# --- the publish → revalidation hook (acceptance #1) --------------------------


async def test_publish_writes_revalidation_outbox_row(client: httpx.AsyncClient) -> None:
    """Publishing writes a knowledge.article.published outbox row naming the paths to revalidate."""
    from relay.core.ids import IdPrefix, decode_public_id
    from relay.core.outbox import OutboxMessage
    from relay.modules.knowledge import events

    tok, _ws, slug = await _owner(client, "Reval")
    art = await _create_article(client, tok, title="Live article")
    await client.post(f"/v0/articles/{art['id']}/publish", headers=_auth(tok))

    # The outbox is infrastructure (no RLS, not truncated between tests) — scope the assertion
    # to THIS article's aggregate_id so other tests' events can't contaminate it.
    aid = decode_public_id(IdPrefix.ARTICLE, art["id"])
    async with session_scope() as session:
        rows = (
            await session.scalars(select(OutboxMessage).where(OutboxMessage.aggregate_id == aid))
        ).all()
    assert len(rows) == 1
    assert rows[0].topic == events.ARTICLE_PUBLISHED
    payload = rows[0].payload
    assert payload["slug"] == slug
    assert f"/hc/{slug}/articles/{art['slug']}" in payload["paths"]
    assert f"/hc/{slug}" in payload["paths"]


async def test_draft_edit_does_not_trigger_revalidation(client: httpx.AsyncClient) -> None:
    from relay.core.ids import IdPrefix, decode_public_id
    from relay.core.outbox import OutboxMessage

    tok, _ws, _slug = await _owner(client, "NoReval")
    art = await _create_article(client, tok, title="Draft only")
    # Editing a draft must not touch the public site.
    await client.patch(
        f"/v0/articles/{art['id']}", json={"title": "Draft renamed"}, headers=_auth(tok)
    )
    # No outbox event of any kind should exist for this (never-published) article.
    aid = decode_public_id(IdPrefix.ARTICLE, art["id"])
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(OutboxMessage).where(OutboxMessage.aggregate_id == aid)
        )
    assert count == 0


async def test_revalidation_consumer_forwards_only_article_paths(client: httpx.AsyncClient) -> None:
    """The consumer forwards paths for article-lifecycle events and ignores unrelated topics."""
    from relay.core.outbox import OUTBOX_STREAM
    from relay.core.redis import get_redis
    from relay.modules.knowledge import events, revalidation

    redis = get_redis()
    await revalidation.ensure_group(redis)
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": "evt-article",
            "aggregate": "article",
            "aggregate_id": str(uuid4()),
            "seq": "1",
            "topic": events.ARTICLE_PUBLISHED,
            "payload": json.dumps({"paths": ["/hc/acme", "/hc/acme/articles/x"]}),
        },
    )
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": "evt-other",
            "aggregate": "conversation",
            "aggregate_id": str(uuid4()),
            "seq": "1",
            "topic": "conversation.part.created",
            "payload": json.dumps({"paths": ["/should/not/fire"]}),
        },
    )

    captured: list[list[str]] = []

    async def _capture(paths: list[str]) -> None:
        captured.append(paths)

    handled = await revalidation.consume_once(redis, _capture, block_ms=200)
    assert handled == 1
    assert captured == [["/hc/acme", "/hc/acme/articles/x"]]


# --- tenancy (master rule 1) --------------------------------------------------


async def test_cross_tenant_help_center_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a, slug_a = await _owner(client, "AlphaHC")
    tok_b, _ws_b, slug_b = await _owner(client, "BravoHC")

    # Both publish an article that happens to share the slug "welcome".
    a = await _create_article(client, tok_a, title="Welcome", body=_para("Alpha content."))
    b = await _create_article(client, tok_b, title="Welcome", body=_para("Bravo content."))
    await client.post(f"/v0/articles/{a['id']}/publish", headers=_auth(tok_a))
    await client.post(f"/v0/articles/{b['id']}/publish", headers=_auth(tok_b))
    assert a["slug"] == b["slug"] == "welcome"

    # Each slug resolves to its own workspace's article — never the other's.
    ra = await client.get(f"/v0/hc/{slug_a}/articles/welcome")
    rb = await client.get(f"/v0/hc/{slug_b}/articles/welcome")
    assert ra.json()["title"] == "Welcome" and ra.json()["id"] == a["id"]
    assert rb.json()["id"] == b["id"]
    assert ra.json()["id"] != rb.json()["id"]

    # B's admin cannot read A's article by id (RLS hides it → 404).
    assert (await client.get(f"/v0/articles/{a['id']}", headers=_auth(tok_b))).status_code == 404
    # B's public search never surfaces A's content.
    sb = await client.get(f"/v0/hc/{slug_b}/search", params={"q": "Alpha"})
    assert sb.json()["results"] == []


async def test_articles_unset_guc_returns_zero_rows(client: httpx.AsyncClient) -> None:
    """RLS backstop: with no app.ws set, the articles table returns nothing."""
    from relay.modules.knowledge.models import Article

    tok, _ws, _slug = await _owner(client, "HasArticles")
    await _create_article(client, tok, title="Something")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # Deliberately do NOT set app.ws.
        count = await session.scalar(select(func.count()).select_from(Article))
    assert count == 0


# --- robustness: revalidation consumer + upsert + collection cycles -----------


async def test_revalidation_consumer_drops_malformed_entry(client: httpx.AsyncClient) -> None:
    """A poison entry (missing outbox_id / non-JSON payload) is acked-and-dropped, never crashing
    the consumer or blocking the stream."""
    from relay.core.outbox import OUTBOX_STREAM
    from relay.core.redis import get_redis
    from relay.modules.knowledge import events, revalidation

    redis = get_redis()
    await revalidation.ensure_group(redis)
    await redis.xadd(OUTBOX_STREAM, {"topic": events.ARTICLE_PUBLISHED, "payload": "not-json"})

    captured: list[list[str]] = []

    async def _cap(paths: list[str]) -> None:
        captured.append(paths)

    handled = await revalidation.consume_once(redis, _cap, block_ms=200)
    assert handled == 0
    assert captured == []


async def test_revalidation_consumer_retries_after_failed_post(client: httpx.AsyncClient) -> None:
    """A failed POST leaves the entry un-acked (not dropped); a later pending-drain retries it."""
    from relay.core.outbox import OUTBOX_STREAM
    from relay.core.redis import get_redis
    from relay.modules.knowledge import events, revalidation

    redis = get_redis()
    await revalidation.ensure_group(redis)
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": "retry-1",
            "topic": events.ARTICLE_PUBLISHED,
            "payload": json.dumps({"paths": ["/hc/x"]}),
        },
    )

    async def _boom(_paths: list[str]) -> None:
        raise RuntimeError("help center site is down")

    # First pass: site down → revalidate raises → entry left un-acked, nothing counted, no crash.
    assert await revalidation.consume_once(redis, _boom, from_id=">", block_ms=200) == 0

    # Retry over this consumer's pending entries with a working callback → succeeds.
    captured: list[list[str]] = []

    async def _ok(paths: list[str]) -> None:
        captured.append(paths)

    assert await revalidation.consume_once(redis, _ok, from_id="0", block_ms=200) == 1
    assert captured == [["/hc/x"]]


async def test_help_center_upsert_keeps_single_row(client: httpx.AsyncClient) -> None:
    """Repeated PATCH /help-center upserts one row (never a duplicate / 500)."""
    from relay.core.ids import IdPrefix, decode_public_id
    from relay.modules.knowledge.models import HelpCenter

    tok, ws_id, _slug = await _owner(client, "HCUpsert")
    for name in ("One", "Two", "Three"):
        r = await client.patch("/v0/help-center", json={"name": name}, headers=_auth(tok))
        assert r.status_code == 200, r.text
    assert (await client.get("/v0/help-center", headers=_auth(tok))).json()["name"] == "Three"

    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws_id)) as session:
        count = await session.scalar(select(func.count()).select_from(HelpCenter))
    assert count == 1


async def test_collection_parent_cycle_rejected(client: httpx.AsyncClient) -> None:
    tok, _ws, _slug = await _owner(client, "Nesting")
    a = (await client.post("/v0/collections", json={"name": "A"}, headers=_auth(tok))).json()
    b = (await client.post("/v0/collections", json={"name": "B"}, headers=_auth(tok))).json()

    # B nested under A is fine.
    r1 = await client.patch(
        f"/v0/collections/{b['id']}", json={"parent_id": a["id"]}, headers=_auth(tok)
    )
    assert r1.status_code == 200, r1.text
    # A under B would form A→B→A — rejected.
    r2 = await client.patch(
        f"/v0/collections/{a['id']}", json={"parent_id": b["id"]}, headers=_auth(tok)
    )
    assert r2.status_code == 409
    # Self-parent — rejected.
    r3 = await client.patch(
        f"/v0/collections/{a['id']}", json={"parent_id": a["id"]}, headers=_auth(tok)
    )
    assert r3.status_code == 409
