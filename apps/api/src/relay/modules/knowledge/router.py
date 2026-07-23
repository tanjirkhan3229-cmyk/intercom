"""HTTP routes for the ``knowledge`` module (RFC-000 §2.5). Mounted by relay.main under ``/v0``.

Two surfaces:
- **Admin** (``/collections``, ``/articles``, ``/help-center``): authenticated via the shared
  kernel dependencies; RBAC enforced in the service layer (the ``authorize`` choke point).
- **Public Help Center** (``/hc/{slug}/...``): *unauthenticated*. The ``{slug}`` (the workspace
  slug = the hosted-site subdomain, ``{workspace-slug}.relayhc.com``) is resolved to a workspace
  via ``identity.service`` — a global-table lookup, no RLS — and the RLS GUC is then set to that
  workspace before any tenant read, so published content is served with full tenant isolation.
  Only published articles are ever exposed here; drafts 404 (admins preview via the admin API).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.db import get_sessionmaker, set_workspace_guc
from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.errors import NotFoundError
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.pagination import Page
from relay.modules.identity import service as identity_service

from . import schemas, service

router = APIRouter(tags=["knowledge"])


# --- Collections (admin) ------------------------------------------------------


@router.post("/collections", response_model=schemas.CollectionOut, status_code=201)
async def create_collection(
    req: schemas.CollectionCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.CollectionOut:
    return await service.create_collection(session, principal, req)


@router.get("/collections", response_model=list[schemas.CollectionOut])
async def list_collections(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.CollectionOut]:
    return await service.list_collections(session)


@router.get("/collections/{collection_id}", response_model=schemas.CollectionOut)
async def get_collection(
    collection_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.CollectionOut:
    return await service.get_collection(session, collection_id)


@router.patch("/collections/{collection_id}", response_model=schemas.CollectionOut)
async def update_collection(
    collection_id: str,
    req: schemas.CollectionUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.CollectionOut:
    return await service.update_collection(session, principal, collection_id, req)


@router.delete("/collections/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_collection(session, principal, collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Articles (admin) ---------------------------------------------------------


@router.post("/articles", response_model=schemas.ArticleOut, status_code=201)
async def create_article(
    req: schemas.ArticleCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ArticleOut:
    return await service.create_article(session, principal, req)


@router.get("/articles", response_model=Page[schemas.ArticleSummary])
async def list_articles(
    _principal: CurrentPrincipal,
    session: SessionDep,
    status: str | None = Query(default=None, pattern="^(draft|published)$"),
    collection_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ArticleSummary]:
    return await service.list_articles(
        session, status=status, collection_id=collection_id, cursor=cursor, limit=limit
    )


@router.get("/articles/{article_id}", response_model=schemas.ArticleOut)
async def get_article(
    article_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.ArticleOut:
    """Editor/admin read — includes drafts (the logged-in-admin preview, P0.8 acceptance #3)."""
    return await service.get_article(session, article_id)


@router.patch("/articles/{article_id}", response_model=schemas.ArticleOut)
async def update_article(
    article_id: str,
    req: schemas.ArticleUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ArticleOut:
    return await service.update_article(session, principal, article_id, req)


@router.delete("/articles/{article_id}", status_code=204)
async def delete_article(
    article_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_article(session, principal, article_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/articles/{article_id}/publish", response_model=schemas.ArticleOut)
async def publish_article(
    article_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ArticleOut:
    return await service.publish_article(session, principal, article_id)


@router.post("/articles/{article_id}/unpublish", response_model=schemas.ArticleOut)
async def unpublish_article(
    article_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ArticleOut:
    return await service.unpublish_article(session, principal, article_id)


# --- Help center config (admin) -----------------------------------------------


@router.get("/help-center", response_model=schemas.HelpCenterOut)
async def get_help_center(
    _principal: CurrentPrincipal, session: SessionDep
) -> schemas.HelpCenterOut:
    return await service.get_help_center(session)


@router.patch("/help-center", response_model=schemas.HelpCenterOut)
async def update_help_center(
    req: schemas.HelpCenterUpdate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.HelpCenterOut:
    return await service.update_help_center(session, principal, req)


# --- Knowledge Hub: external sources + retrieval (admin, P1.1) -----------------


@router.post("/sources", response_model=schemas.SourceOut, status_code=201)
async def create_source(
    req: schemas.SourceCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SourceOut:
    return await service.create_source(session, principal, req)


@router.get("/sources", response_model=list[schemas.SourceOut])
async def list_sources(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.SourceOut]:
    return await service.list_sources(session)


@router.get("/sources/{source_id}", response_model=schemas.SourceOut)
async def get_source(
    source_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.SourceOut:
    return await service.get_source(session, source_id)


@router.patch("/sources/{source_id}", response_model=schemas.SourceOut)
async def update_source(
    source_id: str,
    req: schemas.SourceUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.SourceOut:
    return await service.update_source(session, principal, source_id, req)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_source(session, principal, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sources/{source_id}/sync", response_model=schemas.SourceOut)
async def sync_source(
    source_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SourceOut:
    """Trigger an async (re-)sync + index. Returns the source; poll its ``status`` for readiness."""
    return await service.sync_source(session, principal, source_id)


@router.post("/knowledge/search", response_model=schemas.RetrievalResponse)
async def search_knowledge(
    req: schemas.RetrievalRequest, principal: CurrentPrincipal, session: SessionDep
) -> schemas.RetrievalResponse:
    """Hybrid retrieval debug surface (the "why did it retrieve that?" view) — any teammate."""
    return await service.search_knowledge(session, principal, req)


@router.post("/knowledge/reembed")
async def reembed(
    req: schemas.ReembedRequest, principal: CurrentPrincipal, session: SessionDep
) -> dict[str, int | str]:
    """Enqueue a dual-version re-embed with atomic per-workspace cutover (RFC-003 §4)."""
    return await service.reembed(session, principal, req)


# --- Public Help Center (unauthenticated, slug-resolved) ----------------------


@dataclass
class PublicHelpCenterCtx:
    """A resolved public Help Center request: an open, workspace-scoped session + workspace id."""

    session: AsyncSession
    workspace_id: uuid.UUID
    name: str
    slug: str


async def _resolve_public_workspace(
    session: AsyncSession, slug_or_app_id: str
) -> identity_service.WorkspaceRef | None:
    """Resolve the public identifier to a workspace: its slug (hosted-site subdomain) first,
    then — as a fallback — a workspace public id (``wrk_…``), so the embedded widget, which
    boots with ``app_id`` rather than the slug, can reach the same published content."""
    ws = await identity_service.get_workspace_by_slug(session, slug_or_app_id)
    if ws is not None:
        return ws
    try:
        workspace_id = decode_public_id(IdPrefix.WORKSPACE, slug_or_app_id)
    except ValueError:
        return None
    return await identity_service.get_workspace_ref(session, workspace_id)


async def _public_ctx(slug: str) -> AsyncIterator[PublicHelpCenterCtx]:
    """Resolve ``slug`` → workspace (global lookup, no RLS), then open a session pinned to it.

    A 404 for an unknown slug is raised *before* yielding, so the public API never leaks which
    workspaces exist beyond a plain not-found. After ``set_workspace_guc`` every read in the
    handler is RLS-scoped to this one workspace.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        ws = await _resolve_public_workspace(session, slug)
        if ws is None:
            raise NotFoundError("help center not found")
        await set_workspace_guc(session, ws.id)
        yield PublicHelpCenterCtx(session=session, workspace_id=ws.id, name=ws.name, slug=ws.slug)


PublicCtx = Annotated[PublicHelpCenterCtx, Depends(_public_ctx)]


@router.get("/hc/{slug}", response_model=schemas.PublicHelpCenterOut)
async def public_help_center(ctx: PublicCtx) -> schemas.PublicHelpCenterOut:
    return await service.public_help_center(ctx.session, ctx.slug, ctx.name)


@router.get("/hc/{slug}/search", response_model=schemas.PublicSearchResponse)
async def public_search(
    ctx: PublicCtx,
    q: str = Query(min_length=1, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> schemas.PublicSearchResponse:
    return await service.public_search(ctx.session, q, limit=limit)


@router.get("/hc/{slug}/articles", response_model=Page[schemas.PublicArticleSummary])
async def public_list_articles(
    ctx: PublicCtx,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.PublicArticleSummary]:
    """All published articles (keyset) — the hosted site builds its sitemap from this."""
    return await service.public_list_articles(ctx.session, cursor=cursor, limit=limit)


@router.get("/hc/{slug}/articles/{article_slug}", response_model=schemas.PublicArticleOut)
async def public_article(ctx: PublicCtx, article_slug: str) -> schemas.PublicArticleOut:
    return await service.public_article(ctx.session, article_slug)


@router.get("/hc/{slug}/collections/{collection_slug}", response_model=schemas.PublicCollectionOut)
async def public_collection(ctx: PublicCtx, collection_slug: str) -> schemas.PublicCollectionOut:
    return await service.public_collection(ctx.session, collection_slug)
