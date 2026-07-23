"""Service layer for the ``knowledge`` module — the cross-module interface (RFC-000 §2.5).

The ONLY surface other modules may import (plus ``events``). Two audiences:

- **Admin** (authenticated editor): collection + article CRUD, publish/unpublish. Drafts are
  visible here (the "preview unpublished as a logged-in admin" path, P0.8 acceptance). Writes
  require ``admin`` role; reads require any authenticated teammate.
- **Public** (unauthenticated hosted site + widget): published content only, addressed by
  slug, with FTS search. RLS scopes every read to the workspace resolved from the slug.

Consistency spine (master rule 2): publishing / unpublishing / editing / deleting a
**published** article writes a ``knowledge.article.*`` outbox row in the same transaction as
the domain write; the ``help-center-revalidate`` consumer turns that into an ISR revalidation
so the live site reflects the change within seconds (P0.8 acceptance #1). The article row is
UPDATEd before ``emit`` (holding its row lock), so ``outbox.emit``'s ``MAX(seq)+1`` is race-free
— the same pattern messaging's W1 uses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.errors import ConflictError, NotFoundError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.identity import service as identity_service

from . import events, indexing, retrieval, schemas
from .blocks import blocks_to_text, excerpt, slugify
from .models import Article, Collection, ExternalSource, HelpCenter


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- DTO builders -------------------------------------------------------------


def collection_out(c: Collection, article_count: int) -> schemas.CollectionOut:
    return schemas.CollectionOut(
        id=encode_public_id(IdPrefix.COLLECTION, c.id),
        slug=c.slug,
        name=c.name,
        description=c.description,
        icon=c.icon,
        position=c.position,
        parent_id=encode_public_id(IdPrefix.COLLECTION, c.parent_id) if c.parent_id else None,
        article_count=article_count,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def article_out(a: Article) -> schemas.ArticleOut:
    return schemas.ArticleOut(
        id=encode_public_id(IdPrefix.ARTICLE, a.id),
        collection_id=(
            encode_public_id(IdPrefix.COLLECTION, a.collection_id) if a.collection_id else None
        ),
        slug=a.slug,
        title=a.title,
        body=a.body,
        status=a.status,
        locale=a.locale,
        seo_title=a.seo_title,
        seo_description=a.seo_description,
        author_id=encode_public_id(IdPrefix.ADMIN, a.author_id) if a.author_id else None,
        position=a.position,
        published_at=a.published_at,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def article_summary(a: Article) -> schemas.ArticleSummary:
    return schemas.ArticleSummary(
        id=encode_public_id(IdPrefix.ARTICLE, a.id),
        collection_id=(
            encode_public_id(IdPrefix.COLLECTION, a.collection_id) if a.collection_id else None
        ),
        slug=a.slug,
        title=a.title,
        status=a.status,
        position=a.position,
        updated_at=a.updated_at,
        published_at=a.published_at,
    )


def help_center_out(hc: HelpCenter | None) -> schemas.HelpCenterOut:
    if hc is None:
        return schemas.HelpCenterOut(
            name=None,
            logo_url=None,
            primary_color=None,
            custom_domain=None,
            default_locale="en",
            updated_at=None,
        )
    return schemas.HelpCenterOut(
        name=hc.name,
        logo_url=hc.logo_url,
        primary_color=hc.primary_color,
        custom_domain=hc.custom_domain,
        default_locale=hc.default_locale,
        updated_at=hc.updated_at,
    )


# --- Internal lookups + slug uniqueness (RLS-scoped) --------------------------


async def _get_collection(session: AsyncSession, collection_id: uuid.UUID) -> Collection:
    collection = await session.get(Collection, collection_id)
    if collection is None:
        raise NotFoundError("collection not found")
    return collection


async def _get_article(session: AsyncSession, article_id: uuid.UUID) -> Article:
    article = await session.get(Article, article_id)
    if article is None or article.deleted_at is not None:
        raise NotFoundError("article not found")
    return article


async def _unique_collection_slug(
    session: AsyncSession, base: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    candidate, n = base, 1
    while True:
        stmt = select(Collection.id).where(Collection.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(Collection.id != exclude_id)
        if await session.scalar(stmt) is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


async def _unique_article_slug(
    session: AsyncSession, base: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    candidate, n = base, 1
    while True:
        stmt = select(Article.id).where(Article.slug == candidate, Article.deleted_at.is_(None))
        if exclude_id is not None:
            stmt = stmt.where(Article.id != exclude_id)
        if await session.scalar(stmt) is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


async def _article_counts(session: AsyncSession, *, published_only: bool) -> dict[uuid.UUID, int]:
    """Per-collection article counts (non-deleted; published-only when asked)."""
    stmt = select(Article.collection_id, func.count()).where(
        Article.deleted_at.is_(None), Article.collection_id.isnot(None)
    )
    if published_only:
        stmt = stmt.where(Article.status == "published")
    stmt = stmt.group_by(Article.collection_id)
    return {cid: n for cid, n in (await session.execute(stmt)).all() if cid is not None}


async def _collection_slugs(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Collection.id, Collection.slug).where(Collection.id.in_(ids))
    )
    return dict(rows.tuples().all())


# --- Revalidation (the publish → ISR hook, master rule 2) ---------------------


def _revalidation_paths(slug: str, article_slug: str, collection_slug: str | None) -> list[str]:
    """The public Next.js routes affected by a published-article change (P0.8)."""
    paths = [f"/hc/{slug}", f"/hc/{slug}/articles/{article_slug}"]
    if collection_slug:
        paths.append(f"/hc/{slug}/collections/{collection_slug}")
    return paths


async def _emit_article_event(
    session: AsyncSession, principal: Principal, article: Article, topic: str
) -> None:
    """Write the outbox row that drives Help Center revalidation (same txn as the write)."""
    ws = await identity_service.get_workspace_ref(session, principal.workspace_id)
    if ws is None:  # defensive: the workspace always exists for an authenticated principal
        return
    collection_slug: str | None = None
    if article.collection_id is not None:
        collection = await session.get(Collection, article.collection_id)
        collection_slug = collection.slug if collection is not None else None
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_ARTICLE,
        aggregate_id=article.id,
        topic=topic,
        payload={
            "workspace_id": str(principal.workspace_id),
            "slug": ws.slug,
            "article_slug": article.slug,
            "collection_slug": collection_slug,
            "paths": _revalidation_paths(ws.slug, article.slug, collection_slug),
        },
    )


# --- Collections (admin) ------------------------------------------------------


async def _decode_parent(session: AsyncSession, parent_public_id: str | None) -> uuid.UUID | None:
    if not parent_public_id:
        return None
    parent_id = _decode_or_404(IdPrefix.COLLECTION, parent_public_id, "parent collection")
    await _get_collection(session, parent_id)  # 404 if missing / other tenant (RLS)
    return parent_id


async def _guard_no_cycle(
    session: AsyncSession, collection_id: uuid.UUID, parent_id: uuid.UUID | None
) -> None:
    """Reject a parent that would create a cycle — self, or any descendant of this collection.

    Nesting is a shipped schema feature (``collections.parent_id``); an A→B→A cycle would trap
    a future recursive breadcrumb/tree render in an infinite loop, so it is refused at write time.
    """
    ancestor = parent_id
    seen: set[uuid.UUID] = set()
    while ancestor is not None:
        if ancestor == collection_id:
            raise ConflictError("a collection cannot be nested under itself or its descendants")
        if ancestor in seen:
            break  # a pre-existing cycle in the data — stop walking
        seen.add(ancestor)
        parent = await session.get(Collection, ancestor)
        ancestor = parent.parent_id if parent is not None else None


async def create_collection(
    session: AsyncSession, principal: Principal, req: schemas.CollectionCreate
) -> schemas.CollectionOut:
    authorize(principal, min_role=Role.ADMIN)
    parent_id = await _decode_parent(session, req.parent_id)
    slug = await _unique_collection_slug(session, slugify(req.slug or req.name))
    collection = Collection(
        workspace_id=principal.workspace_id,
        slug=slug,
        name=req.name,
        description=req.description,
        icon=req.icon,
        position=req.position,
        parent_id=parent_id,
    )
    session.add(collection)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("a collection with this slug already exists") from exc
    return collection_out(collection, article_count=0)


async def list_collections(session: AsyncSession) -> list[schemas.CollectionOut]:
    counts = await _article_counts(session, published_only=False)
    collections = (
        await session.scalars(select(Collection).order_by(Collection.position, Collection.name))
    ).all()
    return [collection_out(c, counts.get(c.id, 0)) for c in collections]


async def get_collection(session: AsyncSession, public_id: str) -> schemas.CollectionOut:
    cid = _decode_or_404(IdPrefix.COLLECTION, public_id, "collection")
    collection = await _get_collection(session, cid)
    counts = await _article_counts(session, published_only=False)
    return collection_out(collection, counts.get(collection.id, 0))


async def update_collection(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.CollectionUpdate
) -> schemas.CollectionOut:
    authorize(principal, min_role=Role.ADMIN)
    cid = _decode_or_404(IdPrefix.COLLECTION, public_id, "collection")
    collection = await _get_collection(session, cid)

    if req.slug is not None:
        collection.slug = await _unique_collection_slug(
            session, slugify(req.slug), exclude_id=collection.id
        )
    if req.parent_id is not None:
        parent_id = await _decode_parent(session, req.parent_id)
        await _guard_no_cycle(session, collection.id, parent_id)
        collection.parent_id = parent_id
    for field in ("name", "description", "icon", "position"):
        val = getattr(req, field)
        if val is not None:
            setattr(collection, field, val)
    collection.updated_at = _now()  # concrete value; avoids expired-attribute refresh (see below)
    await session.flush()
    counts = await _article_counts(session, published_only=False)
    return collection_out(collection, counts.get(collection.id, 0))


async def delete_collection(session: AsyncSession, principal: Principal, public_id: str) -> None:
    """Delete a collection. Articles' ``collection_id`` is set NULL (FK ON DELETE SET NULL)."""
    authorize(principal, min_role=Role.ADMIN)
    cid = _decode_or_404(IdPrefix.COLLECTION, public_id, "collection")
    collection = await _get_collection(session, cid)
    await session.delete(collection)
    await session.flush()


# --- Articles (admin) ---------------------------------------------------------


async def _decode_collection_ref(
    session: AsyncSession, collection_public_id: str | None
) -> uuid.UUID | None:
    if not collection_public_id:
        return None
    cid = _decode_or_404(IdPrefix.COLLECTION, collection_public_id, "collection")
    await _get_collection(session, cid)
    return cid


async def create_article(
    session: AsyncSession, principal: Principal, req: schemas.ArticleCreate
) -> schemas.ArticleOut:
    authorize(principal, min_role=Role.ADMIN)
    collection_id = await _decode_collection_ref(session, req.collection_id)
    slug = await _unique_article_slug(session, slugify(req.slug or req.title))
    article = Article(
        workspace_id=principal.workspace_id,
        collection_id=collection_id,
        slug=slug,
        title=req.title,
        body=req.body,
        body_text=blocks_to_text(req.body),
        status="draft",
        locale=req.locale,
        seo_title=req.seo_title,
        seo_description=req.seo_description,
        author_id=principal.admin_id,
        position=req.position,
    )
    session.add(article)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("an article with this slug already exists") from exc
    return article_out(article)


async def list_articles(
    session: AsyncSession,
    *,
    status: str | None = None,
    collection_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ArticleSummary]:
    n = clamp_limit(limit)
    stmt = select(Article).where(Article.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Article.status == status)
    if collection_id is not None:
        cid = _decode_or_404(IdPrefix.COLLECTION, collection_id, "collection")
        stmt = stmt.where(Article.collection_id == cid)
    if cursor:
        cur = _decode_or_404(IdPrefix.ARTICLE, cursor, "cursor")
        stmt = stmt.where(Article.id < cur)
    articles = (await session.scalars(stmt.order_by(Article.id.desc()).limit(n + 1))).all()
    next_cursor = None
    if len(articles) > n:
        articles = list(articles[:n])
        next_cursor = encode_public_id(IdPrefix.ARTICLE, articles[-1].id)
    return Page(items=[article_summary(a) for a in articles], next_cursor=next_cursor)


async def get_article(session: AsyncSession, public_id: str) -> schemas.ArticleOut:
    """Admin/editor read — returns drafts too (the logged-in-admin preview path)."""
    aid = _decode_or_404(IdPrefix.ARTICLE, public_id, "article")
    return article_out(await _get_article(session, aid))


async def update_article(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.ArticleUpdate
) -> schemas.ArticleOut:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.ARTICLE, public_id, "article")
    article = await _get_article(session, aid)

    if req.slug is not None:
        article.slug = await _unique_article_slug(session, slugify(req.slug), exclude_id=article.id)
    if req.collection_id is not None:
        # Empty string clears the collection; a real id (re)assigns it.
        article.collection_id = await _decode_collection_ref(session, req.collection_id or None)
    if req.body is not None:
        article.body = req.body
        article.body_text = blocks_to_text(req.body)
    for field in ("title", "seo_title", "seo_description", "position"):
        val = getattr(req, field)
        if val is not None:
            setattr(article, field, val)
    # Set updated_at to a concrete value (not the func.now() onupdate) so the attribute is not
    # left expired after the UPDATE — async SQLAlchemy forbids the implicit refresh-on-access.
    article.updated_at = _now()
    await session.flush()
    # A live article changed → refresh the ISR site. Draft edits do not touch the public site.
    if article.status == "published":
        await _emit_article_event(session, principal, article, events.ARTICLE_UPDATED)
    return article_out(article)


async def delete_article(session: AsyncSession, principal: Principal, public_id: str) -> None:
    """Soft delete (``deleted_at``); frees the partial-unique slug for reuse."""
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.ARTICLE, public_id, "article")
    article = await _get_article(session, aid)
    was_published = article.status == "published"
    article.deleted_at = _now()
    await session.flush()
    if was_published:
        await _emit_article_event(session, principal, article, events.ARTICLE_DELETED)


async def publish_article(
    session: AsyncSession, principal: Principal, public_id: str
) -> schemas.ArticleOut:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.ARTICLE, public_id, "article")
    article = await _get_article(session, aid)
    article.status = "published"
    if article.published_at is None:
        article.published_at = _now()
    article.updated_at = _now()
    await session.flush()
    await _emit_article_event(session, principal, article, events.ARTICLE_PUBLISHED)
    return article_out(article)


async def unpublish_article(
    session: AsyncSession, principal: Principal, public_id: str
) -> schemas.ArticleOut:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.ARTICLE, public_id, "article")
    article = await _get_article(session, aid)
    was_published = article.status == "published"
    article.status = "draft"
    article.updated_at = _now()
    await session.flush()
    if was_published:
        await _emit_article_event(session, principal, article, events.ARTICLE_UNPUBLISHED)
    return article_out(article)


# --- Help center config (admin) -----------------------------------------------


async def _get_help_center_row(session: AsyncSession) -> HelpCenter | None:
    result = await session.scalars(select(HelpCenter).limit(1))
    return result.first()


async def get_help_center(session: AsyncSession) -> schemas.HelpCenterOut:
    return help_center_out(await _get_help_center_row(session))


async def update_help_center(
    session: AsyncSession, principal: Principal, req: schemas.HelpCenterUpdate
) -> schemas.HelpCenterOut:
    authorize(principal, min_role=Role.ADMIN)
    provided = {
        f: getattr(req, f)
        for f in ("name", "logo_url", "primary_color", "default_locale")
        if getattr(req, f) is not None
    }
    # Idempotent upsert on the one-row-per-workspace constraint. A get-then-insert would 500 on
    # the create race (two first-time PATCHes racing on uq_help_centers_workspace_id); ON CONFLICT
    # collapses that to a single row. The conflict can only ever be this workspace's own row
    # (workspace_id is unique per row), so RLS + ON CONFLICT compose cleanly.
    stmt = pg_insert(HelpCenter).values(id=uuid7(), workspace_id=principal.workspace_id, **provided)
    if provided:
        stmt = stmt.on_conflict_do_update(
            index_elements=[HelpCenter.workspace_id],
            set_={**provided, "updated_at": _now()},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[HelpCenter.workspace_id])
    await session.execute(stmt)

    hc = await _get_help_center_row(session)
    assert hc is not None  # just upserted a row for this workspace
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_HELP_CENTER,
        aggregate_id=hc.id,
        topic=events.HELP_CENTER_UPDATED,
        payload={"workspace_id": str(principal.workspace_id)},
    )
    return help_center_out(hc)


# --- Public (hosted site + widget) --------------------------------------------


def _public_article_out(a: Article, collection_slug: str | None) -> schemas.PublicArticleOut:
    return schemas.PublicArticleOut(
        id=encode_public_id(IdPrefix.ARTICLE, a.id),
        slug=a.slug,
        title=a.title,
        body=a.body,
        seo_title=a.seo_title,
        seo_description=a.seo_description or excerpt(a.body_text),
        collection_slug=collection_slug,
        published_at=a.published_at,
        updated_at=a.updated_at,
    )


def _public_summary(a: Article, collection_slug: str | None) -> schemas.PublicArticleSummary:
    return schemas.PublicArticleSummary(
        id=encode_public_id(IdPrefix.ARTICLE, a.id),
        slug=a.slug,
        title=a.title,
        excerpt=excerpt(a.body_text),
        collection_slug=collection_slug,
        updated_at=a.updated_at,
    )


async def public_help_center(
    session: AsyncSession, workspace_slug: str, workspace_name: str
) -> schemas.PublicHelpCenterOut:
    hc = await _get_help_center_row(session)
    counts = await _article_counts(session, published_only=True)
    collections = (
        await session.scalars(select(Collection).order_by(Collection.position, Collection.name))
    ).all()
    summaries = [
        schemas.PublicCollectionSummary(
            slug=c.slug,
            name=c.name,
            description=c.description,
            icon=c.icon,
            article_count=counts.get(c.id, 0),
        )
        for c in collections
        if counts.get(c.id, 0) > 0
    ]
    return schemas.PublicHelpCenterOut(
        workspace_slug=workspace_slug,
        name=(hc.name if hc and hc.name else workspace_name),
        logo_url=hc.logo_url if hc else None,
        primary_color=hc.primary_color if hc else None,
        default_locale=hc.default_locale if hc else "en",
        collections=summaries,
    )


async def public_collection(
    session: AsyncSession, collection_slug: str
) -> schemas.PublicCollectionOut:
    collection = await session.scalar(select(Collection).where(Collection.slug == collection_slug))
    if collection is None:
        raise NotFoundError("collection not found")
    articles = (
        await session.scalars(
            select(Article)
            .where(
                Article.collection_id == collection.id,
                Article.status == "published",
                Article.deleted_at.is_(None),
            )
            .order_by(Article.position, Article.title)
        )
    ).all()
    return schemas.PublicCollectionOut(
        slug=collection.slug,
        name=collection.name,
        description=collection.description,
        icon=collection.icon,
        articles=[_public_summary(a, collection.slug) for a in articles],
    )


async def public_article(session: AsyncSession, article_slug: str) -> schemas.PublicArticleOut:
    article = await session.scalar(
        select(Article).where(
            Article.slug == article_slug,
            Article.status == "published",
            Article.deleted_at.is_(None),
        )
    )
    if article is None:
        raise NotFoundError("article not found")
    slugs = await _collection_slugs(
        session, {article.collection_id} if article.collection_id else set()
    )
    collection_slug = slugs.get(article.collection_id) if article.collection_id else None
    return _public_article_out(article, collection_slug)


async def public_search(
    session: AsyncSession, q: str, *, limit: int | None = None
) -> schemas.PublicSearchResponse:
    """FTS over published articles (RFC-002 §5.5 / Appendix B: ``websearch_to_tsquery``).

    Title is weighted ``A`` and body ``B`` in ``search_tsv``, so ``ts_rank`` orders a title
    match above a body-only match (P0.8 acceptance #2).
    """
    query = q.strip()
    if not query:
        return schemas.PublicSearchResponse(query=q, results=[])
    n = clamp_limit(limit)
    tsquery = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank(Article.search_tsv, tsquery)
    stmt = (
        select(Article, rank.label("rank"))
        .where(
            Article.status == "published",
            Article.deleted_at.is_(None),
            Article.search_tsv.bool_op("@@")(tsquery),
        )
        .order_by(rank.desc(), Article.id.desc())
        .limit(n)
    )
    rows = (await session.execute(stmt)).all()
    articles = [a for a, _ in rows]
    slugs = await _collection_slugs(session, {a.collection_id for a in articles if a.collection_id})
    results = [
        schemas.PublicSearchResult(
            slug=a.slug,
            title=a.title,
            excerpt=excerpt(a.body_text),
            collection_slug=slugs.get(a.collection_id) if a.collection_id else None,
            rank=float(r),
        )
        for a, r in rows
    ]
    return schemas.PublicSearchResponse(query=q, results=results)


async def public_list_articles(
    session: AsyncSession, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.PublicArticleSummary]:
    """Published articles, keyset-paginated — powers the hosted site's sitemap."""
    n = clamp_limit(limit)
    stmt = select(Article).where(Article.status == "published", Article.deleted_at.is_(None))
    if cursor:
        cur = _decode_or_404(IdPrefix.ARTICLE, cursor, "cursor")
        stmt = stmt.where(Article.id < cur)
    articles = (await session.scalars(stmt.order_by(Article.id.desc()).limit(n + 1))).all()
    next_cursor = None
    if len(articles) > n:
        articles = list(articles[:n])
        next_cursor = encode_public_id(IdPrefix.ARTICLE, articles[-1].id)
    slugs = await _collection_slugs(session, {a.collection_id for a in articles if a.collection_id})
    items = [
        _public_summary(a, slugs.get(a.collection_id) if a.collection_id else None)
        for a in articles
    ]
    return Page(items=items, next_cursor=next_cursor)


# --- Knowledge Hub: external sources (admin, P1.1) ----------------------------


def source_out(s: ExternalSource) -> schemas.SourceOut:
    return schemas.SourceOut(
        id=encode_public_id(IdPrefix.EXTERNAL_SOURCE, s.id),
        kind=s.kind,
        title=s.title,
        status=s.status,
        config=s.config,
        locale=s.locale,
        audience=s.audience,
        document_count=s.document_count,
        chunk_count=s.chunk_count,
        last_synced_at=s.last_synced_at,
        last_error=s.last_error,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _get_source(session: AsyncSession, source_id: uuid.UUID) -> ExternalSource:
    source = await session.get(ExternalSource, source_id)
    if source is None:  # RLS scopes to the workspace, so another tenant's id is a clean 404
        raise NotFoundError("source not found")
    return source


def _validate_source_config(kind: str, config: dict[str, Any]) -> None:
    """Reject a config that the sync would choke on later (fail at write time, not mid-crawl)."""
    if kind == "url" and not str(config.get("url", "")).startswith(("http://", "https://")):
        raise ConflictError("url source requires config.url (http/https)")
    if kind == "pdf" and not config.get("s3_key"):
        raise ConflictError("pdf source requires config.s3_key")
    if kind == "snippet" and not str(config.get("body", "")).strip():
        raise ConflictError("snippet source requires a non-empty config.body")


async def create_source(
    session: AsyncSession, principal: Principal, req: schemas.SourceCreate
) -> schemas.SourceOut:
    authorize(principal, min_role=Role.ADMIN)
    _validate_source_config(req.kind, req.config)
    source = ExternalSource(
        id=uuid7(),
        workspace_id=principal.workspace_id,
        kind=req.kind,
        title=req.title,
        status="pending",
        config=req.config,
        locale=req.locale,
        audience=req.audience,
    )
    session.add(source)
    await session.flush()
    return source_out(source)


async def list_sources(session: AsyncSession) -> list[schemas.SourceOut]:
    sources = (
        await session.scalars(select(ExternalSource).order_by(ExternalSource.created_at.desc()))
    ).all()
    return [source_out(s) for s in sources]


async def get_source(session: AsyncSession, public_id: str) -> schemas.SourceOut:
    sid = _decode_or_404(IdPrefix.EXTERNAL_SOURCE, public_id, "source")
    return source_out(await _get_source(session, sid))


async def update_source(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.SourceUpdate
) -> schemas.SourceOut:
    authorize(principal, min_role=Role.ADMIN)
    sid = _decode_or_404(IdPrefix.EXTERNAL_SOURCE, public_id, "source")
    source = await _get_source(session, sid)
    if req.title is not None:
        source.title = req.title
    if req.config is not None:
        _validate_source_config(source.kind, req.config)
        source.config = req.config
    if req.locale is not None:
        source.locale = req.locale
    if req.audience is not None:
        source.audience = req.audience
    await session.flush()
    return source_out(source)


async def delete_source(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.ADMIN)
    sid = _decode_or_404(IdPrefix.EXTERNAL_SOURCE, public_id, "source")
    source = await _get_source(session, sid)
    await indexing.delete_source_chunks(
        session, workspace_id=principal.workspace_id, source_kind=source.kind, source_id=source.id
    )
    await session.delete(source)


async def sync_source(
    session: AsyncSession, principal: Principal, public_id: str
) -> schemas.SourceOut:
    """Enqueue an async (re-)sync + index of the source (RFC-001 §9: never crawl in the request
    path). The task marks the source ``syncing`` then ``synced``/``error`` as it runs."""
    authorize(principal, min_role=Role.ADMIN)
    sid = _decode_or_404(IdPrefix.EXTERNAL_SOURCE, public_id, "source")
    source = await _get_source(session, sid)
    from relay.worker import celery_app

    celery_app.send_task(
        "knowledge.sync_source",
        args=[str(principal.workspace_id), str(source.id)],
        queue="ai.batch",
    )
    return source_out(source)


# --- Knowledge Hub: retrieval (admin/agent debug + the internal contract for Neko, P1.1) ------


async def retrieve_chunks(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    locale: str = "en",
    k: int | None = None,
    method: retrieval.RetrievalMethod = "hybrid",
    source_kinds: list[str] | None = None,
    ef_search: int | None = None,
) -> list[retrieval.RetrievedChunk]:
    """The cross-module retrieval contract (Neko/copilot call this, RFC-003 §4). RLS-scoped."""
    return await retrieval.retrieve(
        session,
        workspace_id=workspace_id,
        query=query,
        locale=locale,
        k=k,
        method=method,
        source_kinds=source_kinds,
        ef_search=ef_search,
    )


def _chunk_out(c: retrieval.RetrievedChunk) -> schemas.RetrievedChunkOut:
    prefix = IdPrefix.ARTICLE if c.source_kind == "article" else IdPrefix.EXTERNAL_SOURCE
    return schemas.RetrievedChunkOut(
        source_id=encode_public_id(prefix, c.source_id),
        source_kind=c.source_kind,
        title=c.title,
        heading_path=c.heading_path,
        content=c.content,
        score=c.score,
    )


async def search_knowledge(
    session: AsyncSession, principal: Principal, req: schemas.RetrievalRequest
) -> schemas.RetrievalResponse:
    """Admin/agent-facing retrieval debug endpoint (the "why did it retrieve that?" surface)."""
    authorize(principal, min_role=Role.AGENT)
    chunks = await retrieve_chunks(
        session,
        workspace_id=principal.workspace_id,
        query=req.query,
        locale=req.locale,
        k=req.k,
        method=req.method,
        source_kinds=req.source_kinds,
        ef_search=req.ef_search,
    )
    return schemas.RetrievalResponse(
        query=req.query, method=req.method, results=[_chunk_out(c) for c in chunks]
    )


async def reembed(
    session: AsyncSession, principal: Principal, req: schemas.ReembedRequest
) -> dict[str, int | str]:
    """Enqueue a dual-version re-embed with atomic per-workspace cutover (RFC-003 §4)."""
    authorize(principal, min_role=Role.ADMIN)
    from relay.worker import celery_app

    celery_app.send_task(
        "knowledge.reembed_workspace",
        args=[str(principal.workspace_id), req.new_version],
        queue="ai.batch",
    )
    return {"status": "queued", "new_version": req.new_version}
