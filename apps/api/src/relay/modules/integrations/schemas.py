"""Pydantic request/response models for the ``integrations`` API (P1.9)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class SlackConnect(BaseModel):
    """Connect a Slack workspace (v0: paste a bot token + signing secret from a manually-created
    Slack app). Secrets are encrypted at rest and never returned."""

    team_id: str = Field(min_length=1, max_length=64)
    team_name: str | None = Field(default=None, max_length=255)
    channel_id: str = Field(min_length=1, max_length=64)
    channel_name: str | None = Field(default=None, max_length=255)
    bot_token: str = Field(min_length=1, max_length=512)
    signing_secret: str = Field(min_length=1, max_length=512)


class IntegrationOut(BaseModel):
    id: str
    integration_type: str
    status: str
    # Non-secret config surfaced for display (never the tokens).
    team_id: str | None
    team_name: str | None
    channel_id: str | None
    channel_name: str | None
    created_at: dt.datetime


class IntegrationStatusUpdate(BaseModel):
    status: str = Field(pattern="^(active|paused|disabled)$")


class ZapierSubscribe(BaseModel):
    topic: str = Field(min_length=1, max_length=64)
    target_url: str = Field(min_length=1, max_length=2048)


class ZapierSubscribeOut(BaseModel):
    id: str
    topic: str
    target_url: str


class ZapierAuthTestOut(BaseModel):
    ok: bool
    workspace_id: str
