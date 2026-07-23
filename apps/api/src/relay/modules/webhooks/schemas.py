"""Pydantic request/response schemas for the ``webhooks`` public API (P0.11)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator

from . import events


def _validate_topics(v: list[str]) -> list[str]:
    unknown = sorted(set(v) - events.WEBHOOK_TOPICS)
    if unknown:
        raise ValueError(f"unknown topic(s): {', '.join(unknown)}")
    return list(dict.fromkeys(v))  # dedupe, preserve order


class WebhookSubscriptionCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    topics: list[str] = Field(min_length=1)

    @field_validator("topics")
    @classmethod
    def _topics_known(cls, v: list[str]) -> list[str]:
        return _validate_topics(v)


class WebhookSubscriptionUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    topics: list[str] | None = Field(default=None, min_length=1)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")

    @field_validator("topics")
    @classmethod
    def _topics_known(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validate_topics(v)


class WebhookSubscriptionOut(BaseModel):
    id: str
    url: str
    topics: list[str]
    status: str
    secret_last4: str
    consecutive_failures: int
    last_error: str | None
    last_success_at: dt.datetime | None
    disabled_at: dt.datetime | None
    created_at: dt.datetime


class WebhookSubscriptionCreated(WebhookSubscriptionOut):
    """Returned once on create / rotate-secret; carries the plaintext signing secret."""

    secret: str


class WebhookDeliveryOut(BaseModel):
    id: str
    subscription_id: str
    topic: str
    status: str
    attempt: int
    response_code: int | None
    error: str | None
    next_attempt_at: dt.datetime | None
    delivered_at: dt.datetime | None
    created_at: dt.datetime
