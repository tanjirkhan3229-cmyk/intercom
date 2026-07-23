"""Pydantic request/response models for the ``channels`` (email) API. IDs are prefixed base62."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, EmailStr, Field

# --- Domains ------------------------------------------------------------------


class DomainCreate(BaseModel):
    domain: str = Field(min_length=3, max_length=255)


class DomainOut(BaseModel):
    id: str
    domain: str
    status: str
    spf_ok: bool
    dmarc_ok: bool
    dns_records: list[dict[str, Any]]
    verified_at: dt.datetime | None
    created_at: dt.datetime


# --- Channel accounts (inbound addresses) -------------------------------------


class AccountCreate(BaseModel):
    address: EmailStr
    domain_id: str | None = None


class AccountOut(BaseModel):
    id: str
    address: str
    domain_id: str | None
    status: str
    created_at: dt.datetime


class AccountStatusUpdate(BaseModel):
    # 'paused' is the per-tenant send-pause switch (RFC-001 §9).
    status: str = Field(pattern="^(active|paused|disabled)$")


# --- Suppressions -------------------------------------------------------------


class SuppressionCreate(BaseModel):
    email: EmailStr
    reason: str = Field(default="manual", pattern="^(bounce|complaint|manual)$")


class SuppressionOut(BaseModel):
    id: str
    email: str
    reason: str
    source: str | None
    created_at: dt.datetime
