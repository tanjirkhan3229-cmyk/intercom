"""Pydantic request/response models for the ``knowledge`` API.

Two audiences:
- **Admin** (authenticated editor): full article/collection/help-center CRUD, drafts visible.
  IDs are prefixed base62 strings (``art_``/``col_``).
- **Public** (unauthenticated hosted site + widget): published content only, addressed by
  human ``slug``s, with theming for rendering. No draft ever crosses this boundary.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_HEX_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
_LOCALE_PATTERN = r"^[a-z]{2}(?:-[A-Z]{2})?$"

# --- Collections (admin) ------------------------------------------------------


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=64)
    position: int = Field(default=0, ge=0)
    parent_id: str | None = None


class CollectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=64)
    position: int | None = Field(default=None, ge=0)
    parent_id: str | None = None


class CollectionOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    icon: str | None
    position: int
    parent_id: str | None
    article_count: int
    created_at: dt.datetime
    updated_at: dt.datetime


# --- Articles (admin) ---------------------------------------------------------


class ArticleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN, max_length=255)
    collection_id: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)
    locale: str = Field(default="en", pattern=_LOCALE_PATTERN)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=500)
    position: int = Field(default=0, ge=0)


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN, max_length=255)
    collection_id: str | None = None
    body: dict[str, Any] | None = None
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=500)
    position: int | None = Field(default=None, ge=0)


class ArticleOut(BaseModel):
    """Full article, drafts included — the authenticated editor / admin preview view."""

    id: str
    collection_id: str | None
    slug: str
    title: str
    body: dict[str, Any]
    status: str
    locale: str
    seo_title: str | None
    seo_description: str | None
    author_id: str | None
    position: int
    published_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ArticleSummary(BaseModel):
    """Compact article for admin list views."""

    id: str
    collection_id: str | None
    slug: str
    title: str
    status: str
    position: int
    updated_at: dt.datetime
    published_at: dt.datetime | None


# --- Help center config (admin) -----------------------------------------------


class HelpCenterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    logo_url: str | None = Field(default=None, max_length=2000)
    primary_color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    default_locale: str | None = Field(default=None, pattern=_LOCALE_PATTERN)


class HelpCenterOut(BaseModel):
    name: str | None
    logo_url: str | None
    primary_color: str | None
    custom_domain: str | None
    default_locale: str
    updated_at: dt.datetime | None


# --- Public (hosted site + widget) --------------------------------------------


class PublicArticleSummary(BaseModel):
    id: str
    slug: str
    title: str
    excerpt: str
    collection_slug: str | None
    updated_at: dt.datetime


class PublicCollectionOut(BaseModel):
    slug: str
    name: str
    description: str | None
    icon: str | None
    articles: list[PublicArticleSummary]


class PublicCollectionSummary(BaseModel):
    slug: str
    name: str
    description: str | None
    icon: str | None
    article_count: int


class PublicArticleOut(BaseModel):
    id: str
    slug: str
    title: str
    body: dict[str, Any]
    seo_title: str | None
    seo_description: str | None
    collection_slug: str | None
    published_at: dt.datetime | None
    updated_at: dt.datetime


class PublicHelpCenterOut(BaseModel):
    workspace_slug: str
    name: str
    logo_url: str | None
    primary_color: str | None
    default_locale: str
    collections: list[PublicCollectionSummary]


class PublicSearchResult(BaseModel):
    slug: str
    title: str
    excerpt: str
    collection_slug: str | None
    rank: float


class PublicSearchResponse(BaseModel):
    query: str
    results: list[PublicSearchResult]
