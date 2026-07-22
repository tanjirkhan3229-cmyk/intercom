"""Pydantic request/response models for the ``crm`` API. IDs are prefixed base62 strings."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, EmailStr, Field, model_validator

# --- Contacts -----------------------------------------------------------------


class ContactIdentify(BaseModel):
    """Idempotent upsert (RFC-002 W2). At least one identity key is required.

    Resolution order: ``external_id`` first (the tenant's stable id), else ``email`` for
    ``kind='user'``. Provided fields are merged onto the matched contact (see service docstring
    for the merge rules); ``custom`` is validated against ``attribute_definitions``.
    """

    external_id: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    kind: str | None = Field(default=None, pattern="^(user|lead)$")
    custom: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _require_identity(self) -> ContactIdentify:
        if not self.external_id and not self.email:
            raise ValueError("identify requires at least one of: external_id, email")
        return self


class ContactCreate(BaseModel):
    kind: str = Field(default="lead", pattern="^(user|lead)$")
    external_id: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    custom: dict[str, Any] = Field(default_factory=dict)


class ContactUpdate(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    kind: str | None = Field(default=None, pattern="^(user|lead)$")
    custom: dict[str, Any] | None = None
    last_seen_at: dt.datetime | None = None


class ContactOut(BaseModel):
    id: str
    kind: str
    external_id: str | None
    email: str | None
    phone: str | None
    name: str | None
    custom: dict[str, Any]
    last_seen_at: dt.datetime | None
    created_at: dt.datetime


# --- Companies ----------------------------------------------------------------


class CompanyCreate(BaseModel):
    external_id: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    custom: dict[str, Any] = Field(default_factory=dict)


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    custom: dict[str, Any] | None = None


class CompanyOut(BaseModel):
    id: str
    external_id: str | None
    name: str | None
    domain: str | None
    custom: dict[str, Any]
    created_at: dt.datetime


class ContactCompanyLink(BaseModel):
    company_id: str


# --- Attribute definitions ----------------------------------------------------


class AttributeDefinitionCreate(BaseModel):
    entity: str = Field(default="contact", pattern="^(contact|company)$")
    name: str = Field(min_length=1, max_length=120)
    data_type: str = Field(pattern="^(string|number|boolean|date|list)$")
    label: str | None = Field(default=None, max_length=255)


class AttributeDefinitionOut(BaseModel):
    id: str
    entity: str
    name: str
    data_type: str
    label: str | None
    created_at: dt.datetime


# --- Events -------------------------------------------------------------------


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime | None = None


class TrackRequest(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=10_000)


class TrackResponse(BaseModel):
    accepted: int


class EventOut(BaseModel):
    name: str
    contact_id: str
    properties: dict[str, Any]
    created_at: dt.datetime
