"""HTTP routes for the ``identity`` module (auth, workspace, members, teams, API keys).

Mounted under ``/v0`` by relay.main. Access tokens are returned in the body (kept in memory
by the SPA); refresh tokens live only in an httpOnly cookie scoped to the auth path.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from relay.core.errors import AuthenticationError, NotFoundError
from relay.core.ids import IdPrefix, decode_public_id

from . import oidc, schemas, service
from .dependencies import CurrentPrincipal, SessionDep, require_role
from .principal import Principal
from .rbac import Role
from .service import AuthResult

router = APIRouter(tags=["identity"])

REFRESH_COOKIE = "relay_rt"
REFRESH_COOKIE_PATH = "/v0/auth"
OIDC_STATE_COOKIE = "relay_oidc_state"
OIDC_NONCE_COOKIE = "relay_oidc_nonce"


# --- Cookie helpers -----------------------------------------------------------


def _is_secure() -> bool:
    from relay.settings import get_settings

    return get_settings().is_production


def _set_refresh_cookie(response: Response, value: str) -> None:
    from relay.settings import get_settings

    response.set_cookie(
        REFRESH_COOKIE,
        value,
        max_age=get_settings().refresh_token_ttl_seconds,
        httponly=True,
        secure=_is_secure(),
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _finalize(response: Response, result: AuthResult) -> schemas.TokenResponse:
    _set_refresh_cookie(response, result.refresh_value)
    return result.to_response()


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ua = request.headers.get("User-Agent")
    ip = request.client.host if request.client else None
    return ua, ip


def _decode_or_404(prefix: str, public_id: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError("resource not found") from exc


# --- Auth ---------------------------------------------------------------------


@router.post("/auth/signup", response_model=schemas.TokenResponse, status_code=201)
async def signup(
    req: schemas.SignupRequest, request: Request, response: Response, session: SessionDep
) -> schemas.TokenResponse:
    ua, ip = _client_meta(request)
    result = await service.signup(session, req, user_agent=ua, ip=ip)
    return _finalize(response, result)


@router.post("/auth/login", response_model=schemas.TokenResponse)
async def login(
    req: schemas.LoginRequest, request: Request, response: Response, session: SessionDep
) -> schemas.TokenResponse:
    ua, ip = _client_meta(request)
    ws_id = _decode_or_404(IdPrefix.WORKSPACE, req.workspace_id) if req.workspace_id else None
    result = await service.login(
        session, email=req.email, password=req.password, workspace_id=ws_id, user_agent=ua, ip=ip
    )
    return _finalize(response, result)


@router.post("/auth/refresh", response_model=schemas.TokenResponse)
async def refresh(
    request: Request, response: Response, session: SessionDep
) -> schemas.TokenResponse:
    value = request.cookies.get(REFRESH_COOKIE)
    if not value:
        raise AuthenticationError("missing refresh token")
    ua, ip = _client_meta(request)
    result = await service.refresh(session, refresh_value=value, user_agent=ua, ip=ip)
    return _finalize(response, result)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, session: SessionDep) -> Response:
    value = request.cookies.get(REFRESH_COOKIE)
    if value:
        await service.logout(session, refresh_value=value)
    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=schemas.MeResponse)
async def me(principal: CurrentPrincipal, session: SessionDep) -> schemas.MeResponse:
    from .models import Admin

    ws = await service.get_workspace(session, principal.workspace_id)
    admin = await session.get(Admin, principal.admin_id)
    if admin is None:
        raise AuthenticationError("account not found")
    return schemas.MeResponse(
        admin=service.admin_out(admin), workspace=service.workspace_out(ws), role=principal.role
    )


# --- Google OIDC --------------------------------------------------------------


@router.get("/auth/google/start", response_model=schemas.AuthorizationUrl)
async def google_start(response: Response) -> schemas.AuthorizationUrl:
    state = oidc.new_state()
    nonce = oidc.new_nonce()
    url = oidc.build_authorization_url(state=state, nonce=nonce)
    for name, val in ((OIDC_STATE_COOKIE, state), (OIDC_NONCE_COOKIE, nonce)):
        response.set_cookie(
            name,
            val,
            max_age=600,
            httponly=True,
            secure=_is_secure(),
            samesite="lax",
            path="/v0/auth",
        )
    return schemas.AuthorizationUrl(authorization_url=url)


@router.get("/auth/google/callback", response_model=schemas.TokenResponse)
async def google_callback(
    code: str, state: str, request: Request, response: Response, session: SessionDep
) -> schemas.TokenResponse:
    expected_state = request.cookies.get(OIDC_STATE_COOKIE)
    nonce = request.cookies.get(OIDC_NONCE_COOKIE)
    if not expected_state or state != expected_state or not nonce:
        raise AuthenticationError("invalid OIDC state")

    tokens = await oidc.exchange_code(code)
    id_token = tokens.get("id_token")
    if not id_token:
        raise AuthenticationError("missing id_token")
    identity = oidc.verify_id_token(id_token, expected_nonce=nonce)

    ua, ip = _client_meta(request)
    result = await service.login_with_google(
        session,
        google_sub=identity.sub,
        email=identity.email,
        name=identity.name,
        user_agent=ua,
        ip=ip,
    )
    response.delete_cookie(OIDC_STATE_COOKIE, path="/v0/auth")
    response.delete_cookie(OIDC_NONCE_COOKIE, path="/v0/auth")
    return _finalize(response, result)


# --- Workspace ----------------------------------------------------------------


@router.get("/workspace", response_model=schemas.WorkspaceOut)
async def get_workspace(principal: CurrentPrincipal, session: SessionDep) -> schemas.WorkspaceOut:
    ws = await service.get_workspace(session, principal.workspace_id)
    return service.workspace_out(ws)


@router.patch("/workspace", response_model=schemas.WorkspaceOut)
async def update_workspace(
    req: schemas.WorkspaceUpdate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.WorkspaceOut:
    ws = await service.update_workspace(session, principal, req)
    return service.workspace_out(ws)


# --- Members ------------------------------------------------------------------


@router.get("/members", response_model=list[schemas.MembershipOut])
async def list_members(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.MembershipOut]:
    return await service.list_members(session)


@router.post("/members", response_model=schemas.MembershipOut, status_code=201)
async def invite_member(
    req: schemas.InviteRequest,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> schemas.MembershipOut:
    return await service.invite_member(session, principal, req)


@router.patch("/members/{membership_id}", response_model=schemas.MembershipOut)
async def update_member_role(
    membership_id: str,
    req: schemas.RoleUpdate,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> schemas.MembershipOut:
    mid = _decode_or_404(IdPrefix.MEMBERSHIP, membership_id)
    return await service.update_member_role(session, principal, mid, req.role)


@router.delete("/members/{membership_id}", status_code=204)
async def remove_member(
    membership_id: str,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> Response:
    mid = _decode_or_404(IdPrefix.MEMBERSHIP, membership_id)
    await service.remove_member(session, principal, mid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Teams --------------------------------------------------------------------


@router.get("/teams", response_model=list[schemas.TeamOut])
async def list_teams(_principal: CurrentPrincipal, session: SessionDep) -> list[schemas.TeamOut]:
    return await service.list_teams(session)


@router.post("/teams", response_model=schemas.TeamOut, status_code=201)
async def create_team(
    req: schemas.TeamCreate,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> schemas.TeamOut:
    return await service.create_team(session, principal, req)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> Response:
    tid = _decode_or_404(IdPrefix.TEAM, team_id)
    await service.delete_team(session, principal, tid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- API keys -----------------------------------------------------------------


@router.get("/api-keys", response_model=list[schemas.ApiKeyOut])
async def list_api_keys(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.ApiKeyOut]:
    return await service.list_api_keys(session)


@router.post("/api-keys", response_model=schemas.ApiKeyCreated, status_code=201)
async def create_api_key(
    req: schemas.ApiKeyCreate,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> schemas.ApiKeyCreated:
    return await service.create_api_key(session, principal, req)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    session: SessionDep,
    principal: Principal = Depends(require_role(Role.ADMIN)),
) -> Response:
    kid = _decode_or_404(IdPrefix.API_KEY, key_id)
    await service.revoke_api_key(session, principal, kid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
