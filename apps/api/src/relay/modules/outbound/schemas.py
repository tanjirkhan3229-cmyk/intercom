"""Pydantic request/response schemas for the ``outbound`` module (P1.8).

Public IDs are prefixed base62 strings (``sbt_``, ``cns_``, ``cpn_`` …); the service layer
encodes/decodes them so raw UUIDs never cross the API boundary.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Subscription types ------------------------------------------------------------------------


class SubscriptionTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    kind: Literal["marketing", "transactional"] = "marketing"
    requires_opt_in: bool = False


class SubscriptionTypeOut(BaseModel):
    id: str
    name: str
    description: str | None
    kind: str
    requires_opt_in: bool
    archived_at: dt.datetime | None
    created_at: dt.datetime


# --- Consent -----------------------------------------------------------------------------------


class ConsentSetIn(BaseModel):
    subscription_type_id: str
    state: Literal["subscribed", "unsubscribed"]


class ConsentOut(BaseModel):
    id: str
    contact_id: str
    subscription_type_id: str
    state: str
    source: str
    updated_at: dt.datetime
    created_at: dt.datetime


# --- Campaigns (email broadcasts) --------------------------------------------------------------


class CampaignVersionIn(BaseModel):
    subject: str = Field(min_length=1, max_length=1000)
    mjml: str = Field(min_length=1)
    preheader: str | None = Field(default=None, max_length=1000)
    from_name: str | None = Field(default=None, max_length=200)
    reply_to: str | None = Field(default=None, max_length=320)
    variables: dict[str, Any] = Field(default_factory=dict)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subscription_type_id: str | None = None
    segment: dict[str, Any] = Field(default_factory=dict)
    version: CampaignVersionIn


class CampaignVersionOut(BaseModel):
    id: str
    version: int
    subject: str
    preheader: str | None
    from_name: str | None
    reply_to: str | None
    status: str
    created_at: dt.datetime


class CampaignOut(BaseModel):
    id: str
    name: str
    channel: str
    status: str
    subscription_type_id: str | None
    segment: dict[str, Any]
    active_version_id: str | None
    fired_at: dt.datetime | None
    created_at: dt.datetime


class CampaignStatsOut(BaseModel):
    campaign_id: str
    audience_size: int
    sent: int
    delivered: int
    opened: int
    clicked: int
    bounced: int
    complained: int
    unsubscribed: int
    skipped: int
    failed: int


# --- In-app posts & chats ----------------------------------------------------------------------


class PostCreate(BaseModel):
    kind: Literal["post", "chat"] = "post"
    title: str | None = Field(default=None, max_length=300)
    body: dict[str, Any] = Field(default_factory=dict)
    subscription_type_id: str | None = None
    segment: dict[str, Any] = Field(default_factory=dict)


class PostOut(BaseModel):
    id: str
    kind: str
    title: str | None
    body: dict[str, Any]
    status: str
    subscription_type_id: str | None
    segment: dict[str, Any]
    audience_size: int
    fired_at: dt.datetime | None
    created_at: dt.datetime


class WidgetPostOut(BaseModel):
    """A delivered in-app post surfaced to the widget (catch-up on boot + realtime)."""

    id: str
    receipt_id: str
    kind: str
    title: str | None
    body: dict[str, Any]
    delivered_at: dt.datetime | None
    seen_at: dt.datetime | None
