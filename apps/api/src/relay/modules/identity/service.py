"""Service layer for the ``identity`` module — the cross-module interface + auth internals.

Auth model (RFC-001 §10): argon2 passwords / Google OIDC → short-lived access JWT (15 min)
+ rotating refresh token (httpOnly cookie). All permission checks funnel through
``rbac.authorize`` (one choke point). Refresh tokens embed the workspace in their prefix so
the RLS GUC can be set before any tenant-table read.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.db import session_scope, set_workspace_guc
from relay.core.errors import AuthenticationError, ConflictError, NotFoundError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.security import (
    create_access_token,
    generate_secret,
    hash_password,
    hash_secret,
    verify_password,
)
from relay.settings import get_settings

from . import schemas
from .models import Admin, ApiKey, Membership, RefreshToken, Team, TeamMembership, Workspace
from .principal import Principal
from .rbac import Role, authorize

REFRESH_SEPARATOR = "."
API_KEY_LABEL = "relaysk"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- DTO builders -------------------------------------------------------------


def admin_out(admin: Admin) -> schemas.AdminOut:
    return schemas.AdminOut(
        id=encode_public_id(IdPrefix.ADMIN, admin.id), email=admin.email, name=admin.name
    )


def workspace_out(ws: Workspace) -> schemas.WorkspaceOut:
    return schemas.WorkspaceOut(
        id=encode_public_id(IdPrefix.WORKSPACE, ws.id), name=ws.name, slug=ws.slug
    )


@dataclass
class AuthResult:
    access_token: str
    expires_in: int
    refresh_value: str
    workspace: Workspace
    admin: Admin
    role: str

    def to_response(self) -> schemas.TokenResponse:
        return schemas.TokenResponse(
            access_token=self.access_token,
            expires_in=self.expires_in,
            workspace=workspace_out(self.workspace),
            admin=admin_out(self.admin),
            role=self.role,
        )


# --- Slugs --------------------------------------------------------------------


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


async def _unique_slug(session: AsyncSession, base: str) -> str:
    candidate = base
    n = 1
    while await session.scalar(select(Workspace.id).where(Workspace.slug == candidate)):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# --- Session (token) issuance + parsing ---------------------------------------


async def _issue_session(
    session: AsyncSession,
    *,
    admin: Admin,
    workspace_id: uuid.UUID,
    role: str,
    family_id: uuid.UUID | None,
    user_agent: str | None,
    ip: str | None,
) -> AuthResult:
    settings = get_settings()
    secret = generate_secret()
    ws_public = encode_public_id(IdPrefix.WORKSPACE, workspace_id)
    refresh_value = f"{ws_public}{REFRESH_SEPARATOR}{secret}"

    rt = RefreshToken(
        workspace_id=workspace_id,
        admin_id=admin.id,
        token_hash=hash_secret(refresh_value),
        family_id=family_id or uuid.uuid4(),
        expires_at=_now() + dt.timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=user_agent,
        ip=ip,
    )
    session.add(rt)
    await session.flush()

    access = create_access_token(admin_id=admin.id, workspace_id=workspace_id, role=role)
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None
    return AuthResult(
        access_token=access,
        expires_in=settings.access_token_ttl_seconds,
        refresh_value=refresh_value,
        workspace=ws,
        admin=admin,
        role=role,
    )


def _parse_refresh_workspace(refresh_value: str) -> uuid.UUID:
    prefix, _, _secret = refresh_value.partition(REFRESH_SEPARATOR)
    try:
        return decode_public_id(IdPrefix.WORKSPACE, prefix)
    except ValueError as exc:
        raise AuthenticationError("malformed refresh token") from exc


# --- Login workspace discovery (controlled RLS bypass) ------------------------


async def list_admin_workspaces(
    session: AsyncSession, admin_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """Return (workspace_id, role) for an admin across all workspaces.

    Uses the SECURITY DEFINER function ``identity_admin_workspaces`` (owned by the BYPASSRLS
    migrator) because at login time we don't yet know the workspace, so RLS would hide the
    memberships. The function only ever returns the given admin's own rows.
    """
    rows = await session.execute(
        text("SELECT workspace_id, role FROM identity_admin_workspaces(:aid)"),
        {"aid": str(admin_id)},
    )
    return [(uuid.UUID(str(r[0])), r[1]) for r in rows.all()]


# --- Auth flows ---------------------------------------------------------------


async def signup(
    session: AsyncSession,
    req: schemas.SignupRequest,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    existing = await session.scalar(select(Admin.id).where(Admin.email == req.email))
    if existing is not None:
        raise ConflictError("an account with this email already exists")

    slug = await _unique_slug(session, _slugify(req.workspace_name))
    ws = Workspace(name=req.workspace_name, slug=slug)
    session.add(ws)
    await session.flush()

    admin = Admin(email=req.email, name=req.name, password_hash=hash_password(req.password))
    session.add(admin)
    await session.flush()

    # From here we operate inside the new workspace's RLS scope.
    await set_workspace_guc(session, ws.id)
    session.add(Membership(workspace_id=ws.id, admin_id=admin.id, role=Role.OWNER))
    await session.flush()

    return await _issue_session(
        session,
        admin=admin,
        workspace_id=ws.id,
        role=Role.OWNER,
        family_id=None,
        user_agent=user_agent,
        ip=ip,
    )


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    workspace_id: uuid.UUID | None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    admin = await session.scalar(select(Admin).where(Admin.email == email))
    if admin is None or admin.password_hash is None or not admin.is_active:
        raise AuthenticationError("invalid credentials")
    if not verify_password(password, admin.password_hash):
        raise AuthenticationError("invalid credentials")

    workspaces = await list_admin_workspaces(session, admin.id)
    if not workspaces:
        raise AuthenticationError("account has no workspace membership")

    if workspace_id is not None:
        match = next((w for w in workspaces if w[0] == workspace_id), None)
        if match is None:
            raise AuthenticationError("not a member of that workspace")
        chosen = match
    elif len(workspaces) == 1:
        chosen = workspaces[0]
    else:
        raise ConflictError(
            "multiple workspaces; specify workspace_id",
            details={
                "workspaces": [encode_public_id(IdPrefix.WORKSPACE, w[0]) for w in workspaces]
            },
        )

    ws_id, role = chosen
    await set_workspace_guc(session, ws_id)
    return await _issue_session(
        session,
        admin=admin,
        workspace_id=ws_id,
        role=role,
        family_id=None,
        user_agent=user_agent,
        ip=ip,
    )


async def refresh(
    session: AsyncSession,
    *,
    refresh_value: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    ws_id = _parse_refresh_workspace(refresh_value)
    await set_workspace_guc(session, ws_id)

    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_secret(refresh_value))
    )
    if token is None:
        raise AuthenticationError("invalid refresh token")

    now = _now()
    if token.revoked_at is not None:
        # Reuse of an already-rotated token → assume theft; revoke the whole family. This must
        # PERSIST even though we then raise, so do it in its own committed transaction — the
        # request's transaction rolls back on the raised error.
        async with session_scope(ws_id) as revoke_session:
            await revoke_session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == token.family_id)
                .values(revoked_at=now)
            )
        raise AuthenticationError("refresh token reuse detected")
    if token.expires_at <= now:
        raise AuthenticationError("refresh token expired")

    role = await session.scalar(
        select(Membership.role).where(Membership.admin_id == token.admin_id)
    )
    admin = await session.get(Admin, token.admin_id)
    if role is None or admin is None or not admin.is_active:
        raise AuthenticationError("membership revoked")

    result = await _issue_session(
        session,
        admin=admin,
        workspace_id=ws_id,
        role=role,
        family_id=token.family_id,
        user_agent=user_agent,
        ip=ip,
    )
    # Rotate: retire the presented token, point it at its successor.
    successor = await session.scalar(
        select(RefreshToken.id).where(RefreshToken.token_hash == hash_secret(result.refresh_value))
    )
    token.revoked_at = now
    token.replaced_by = successor
    await session.flush()
    return result


async def login_with_google(
    session: AsyncSession,
    *,
    google_sub: str,
    email: str,
    name: str,
    workspace_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    """Log in an existing teammate via Google OIDC. No self-signup: the account must exist
    (invited into a workspace). Links ``google_sub`` on first OIDC login."""
    admin = await session.scalar(select(Admin).where(Admin.google_sub == google_sub))
    if admin is None:
        admin = await session.scalar(select(Admin).where(Admin.email == email))
        if admin is None:
            raise AuthenticationError(
                "no account for this Google identity; ask an admin to invite you"
            )
        if admin.google_sub is None:
            admin.google_sub = google_sub
    if not admin.is_active:
        raise AuthenticationError("account disabled")
    if not admin.name:
        admin.name = name
    await session.flush()

    workspaces = await list_admin_workspaces(session, admin.id)
    if not workspaces:
        raise AuthenticationError("account has no workspace membership")
    if workspace_id is not None:
        match = next((w for w in workspaces if w[0] == workspace_id), None)
        if match is None:
            raise AuthenticationError("not a member of that workspace")
        chosen = match
    elif len(workspaces) == 1:
        chosen = workspaces[0]
    else:
        raise ConflictError(
            "multiple workspaces; specify workspace_id",
            details={
                "workspaces": [encode_public_id(IdPrefix.WORKSPACE, w[0]) for w in workspaces]
            },
        )

    ws_id, role = chosen
    await set_workspace_guc(session, ws_id)
    return await _issue_session(
        session,
        admin=admin,
        workspace_id=ws_id,
        role=role,
        family_id=None,
        user_agent=user_agent,
        ip=ip,
    )


async def logout(session: AsyncSession, *, refresh_value: str) -> None:
    """Best-effort revoke of the presented token's whole family. Never errors on bad input."""
    try:
        ws_id = _parse_refresh_workspace(refresh_value)
    except AuthenticationError:
        return
    await set_workspace_guc(session, ws_id)
    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_secret(refresh_value))
    )
    if token is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == token.family_id)
            .values(revoked_at=_now())
        )


async def revoke_all_sessions(session: AsyncSession, *, admin_id: uuid.UUID) -> None:
    """Revoke every active refresh token for an admin in the current workspace."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.admin_id == admin_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


# --- Workspace ----------------------------------------------------------------


async def get_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> Workspace:
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise NotFoundError("workspace not found")
    return ws


@dataclass(frozen=True)
class WidgetSettings:
    """Public workspace facts the messenger widget boots with. The ``messenger`` blob is the raw
    ``settings['messenger']`` sub-object (theme/office-hours/expected-reply + the identity-
    verification config); callers must strip the secret before returning it to a client."""

    name: str
    messenger: dict[str, Any]


async def widget_settings(session: AsyncSession, workspace_id: uuid.UUID) -> WidgetSettings:
    """Cross-module accessor (the widget BFF lives in ``messaging``) so no other module imports
    the ``Workspace`` model. Raises ``NotFoundError`` for an unknown ``app_id``."""
    ws = await get_workspace(session, workspace_id)
    messenger = ws.settings.get("messenger") if isinstance(ws.settings, dict) else None
    return WidgetSettings(name=ws.name, messenger=dict(messenger) if messenger else {})


async def update_workspace(
    session: AsyncSession, principal: Principal, update_req: schemas.WorkspaceUpdate
) -> Workspace:
    authorize(principal, min_role=Role.ADMIN)
    ws = await get_workspace(session, principal.workspace_id)
    if update_req.name is not None:
        ws.name = update_req.name
    if update_req.settings is not None:
        ws.settings = {**ws.settings, **update_req.settings}
    await session.flush()
    return ws


# --- Members ------------------------------------------------------------------


async def list_members(session: AsyncSession) -> list[schemas.MembershipOut]:
    rows = await session.execute(
        select(Membership, Admin).join(Admin, Admin.id == Membership.admin_id)
    )
    return [
        schemas.MembershipOut(
            id=encode_public_id(IdPrefix.MEMBERSHIP, m.id),
            admin=admin_out(a),
            role=m.role,
            created_at=m.created_at,
        )
        for m, a in rows.all()
    ]


async def count_active_memberships(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Active-seat count for billing (RFC-002 §5.6): memberships whose admin account is
    active. Read-only cross-module helper — the ``service`` surface billing consults instead
    of importing ``identity.models`` directly (import-linter boundary rule)."""
    count = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .join(Admin, Admin.id == Membership.admin_id)
        .where(Membership.workspace_id == workspace_id, Admin.is_active.is_(True))
    )
    return count or 0


async def get_admin_email(session: AsyncSession, admin_id: uuid.UUID) -> str:
    admin = await session.get(Admin, admin_id)
    if admin is None:
        raise NotFoundError("admin not found")
    return admin.email


async def invite_member(
    session: AsyncSession, principal: Principal, req: schemas.InviteRequest
) -> schemas.MembershipOut:
    authorize(principal, min_role=Role.ADMIN)
    admin = await session.scalar(select(Admin).where(Admin.email == req.email))
    if admin is None:
        admin = Admin(email=req.email, name=req.name, password_hash=None)
        session.add(admin)
        await session.flush()

    dupe = await session.scalar(select(Membership.id).where(Membership.admin_id == admin.id))
    if dupe is not None:
        raise ConflictError("already a member of this workspace")

    membership = Membership(workspace_id=principal.workspace_id, admin_id=admin.id, role=req.role)
    session.add(membership)
    await session.flush()
    # Seat sync (RFC-002 §5.6): keep the local seat count current for the next Stripe push.
    # Deliberate reverse-direction service import (identity -> billing), allowed by the
    # import-linter boundary rule (any module -> any module's `service`).
    from relay.modules.billing import service as billing_service

    await billing_service.recalculate_seats(session, principal.workspace_id)
    return schemas.MembershipOut(
        id=encode_public_id(IdPrefix.MEMBERSHIP, membership.id),
        admin=admin_out(admin),
        role=membership.role,
        created_at=membership.created_at,
    )


async def update_member_role(
    session: AsyncSession, principal: Principal, membership_id: uuid.UUID, role: str
) -> schemas.MembershipOut:
    authorize(principal, min_role=Role.ADMIN)
    membership = await session.get(Membership, membership_id)
    if membership is None:
        raise NotFoundError("membership not found")
    membership.role = role
    await session.flush()
    admin = await session.get(Admin, membership.admin_id)
    assert admin is not None
    return schemas.MembershipOut(
        id=encode_public_id(IdPrefix.MEMBERSHIP, membership.id),
        admin=admin_out(admin),
        role=membership.role,
        created_at=membership.created_at,
    )


async def remove_member(
    session: AsyncSession, principal: Principal, membership_id: uuid.UUID
) -> None:
    authorize(principal, min_role=Role.ADMIN)
    membership = await session.get(Membership, membership_id)
    if membership is None:
        raise NotFoundError("membership not found")
    await session.delete(membership)
    await session.flush()
    from relay.modules.billing import service as billing_service

    await billing_service.recalculate_seats(session, principal.workspace_id)


# --- Teams --------------------------------------------------------------------


async def list_teams(session: AsyncSession) -> list[schemas.TeamOut]:
    teams = (await session.scalars(select(Team).order_by(Team.created_at))).all()
    return [
        schemas.TeamOut(
            id=encode_public_id(IdPrefix.TEAM, t.id), name=t.name, created_at=t.created_at
        )
        for t in teams
    ]


async def create_team(
    session: AsyncSession, principal: Principal, req: schemas.TeamCreate
) -> schemas.TeamOut:
    authorize(principal, min_role=Role.ADMIN)
    existing = await session.scalar(select(Team.id).where(Team.name == req.name))
    if existing is not None:
        raise ConflictError("a team with this name already exists")
    team = Team(workspace_id=principal.workspace_id, name=req.name)
    session.add(team)
    await session.flush()
    return schemas.TeamOut(
        id=encode_public_id(IdPrefix.TEAM, team.id), name=team.name, created_at=team.created_at
    )


async def delete_team(session: AsyncSession, principal: Principal, team_id: uuid.UUID) -> None:
    authorize(principal, min_role=Role.ADMIN)
    team = await session.get(Team, team_id)
    if team is None:
        raise NotFoundError("team not found")
    await session.delete(team)
    await session.flush()


async def team_agent_ids(session: AsyncSession, team_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Assignable admin ids for round-robin (RFC-002 §7 — the ``messaging`` service calls this).

    Members with an assignable role (``owner``/``admin``/``agent``; ``restricted`` excluded);
    scoped to a team when ``team_id`` is given, else the whole workspace. Ordered by ``admin_id``
    so the round-robin rotation is deterministic. RLS scopes every read to the workspace.
    """
    stmt = select(Membership.admin_id).where(
        Membership.role.in_([Role.OWNER, Role.ADMIN, Role.AGENT])
    )
    if team_id is not None:
        stmt = stmt.join(TeamMembership, TeamMembership.membership_id == Membership.id).where(
            TeamMembership.team_id == team_id
        )
    return list((await session.scalars(stmt.order_by(Membership.admin_id))).all())


# --- API keys -----------------------------------------------------------------


def _api_key_out(key: ApiKey) -> schemas.ApiKeyOut:
    return schemas.ApiKeyOut(
        id=encode_public_id(IdPrefix.API_KEY, key.id),
        name=key.name,
        key_prefix=key.key_prefix,
        scopes=list(key.scopes),
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


async def list_api_keys(session: AsyncSession) -> list[schemas.ApiKeyOut]:
    keys = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at))).all()
    return [_api_key_out(k) for k in keys]


async def create_api_key(
    session: AsyncSession, principal: Principal, req: schemas.ApiKeyCreate
) -> schemas.ApiKeyCreated:
    authorize(principal, min_role=Role.ADMIN)
    ws_public = encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
    secret = generate_secret()
    # Embeds the workspace so P0.11 can resolve the tenant before setting the RLS GUC.
    full_key = f"{API_KEY_LABEL}_{ws_public}_{secret}"
    key = ApiKey(
        workspace_id=principal.workspace_id,
        name=req.name,
        key_prefix=full_key[: len(f"{API_KEY_LABEL}_{ws_public}_") + 4],
        key_hash=hash_secret(full_key),
        scopes=req.scopes,
        created_by=principal.admin_id,
    )
    session.add(key)
    await session.flush()
    out = _api_key_out(key)
    return schemas.ApiKeyCreated(**out.model_dump(), key=full_key)


async def revoke_api_key(session: AsyncSession, principal: Principal, key_id: uuid.UUID) -> None:
    authorize(principal, min_role=Role.ADMIN)
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise NotFoundError("api key not found")
    key.revoked_at = _now()
    await session.flush()
