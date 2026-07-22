"""Pydantic request/response models for the identity API. IDs are prefixed base62 strings."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, EmailStr, Field

# --- Auth ---------------------------------------------------------------------


class SignupRequest(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    # Required only when the account belongs to more than one workspace.
    workspace_id: str | None = None


class AdminOut(BaseModel):
    id: str
    email: EmailStr
    name: str


class WorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    workspace: WorkspaceOut
    admin: AdminOut
    role: str


class MeResponse(BaseModel):
    admin: AdminOut
    workspace: WorkspaceOut
    role: str


class AuthorizationUrl(BaseModel):
    authorization_url: str


# --- Workspace / teams / members ----------------------------------------------


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    settings: dict | None = None


class MembershipOut(BaseModel):
    id: str
    admin: AdminOut
    role: str
    created_at: dt.datetime


class InviteRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern="^(owner|admin|agent|restricted)$")


class RoleUpdate(BaseModel):
    role: str = Field(pattern="^(owner|admin|agent|restricted)$")


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TeamOut(BaseModel):
    id: str
    name: str
    created_at: dt.datetime


# --- API keys -----------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    # The full key is returned exactly once, at creation.
    key: str
