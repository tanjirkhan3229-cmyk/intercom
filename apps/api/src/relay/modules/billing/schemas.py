"""Pydantic request/response models for the billing API. IDs are prefixed base62 strings."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class CheckoutSessionCreate(BaseModel):
    plan_code: str


class CheckoutSessionOut(BaseModel):
    url: str


class PortalSessionOut(BaseModel):
    url: str


class SubscriptionOut(BaseModel):
    id: str
    plan_code: str
    status: str
    banner_state: str
    seats: int
    trial_ends_at: dt.datetime | None
    current_period_end: dt.datetime | None
