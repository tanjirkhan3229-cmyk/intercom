"""Unit tests for the RBAC choke point."""

from __future__ import annotations

import uuid

import pytest

from relay.core.errors import PermissionDeniedError
from relay.modules.identity.principal import Principal
from relay.modules.identity.rbac import Role, authorize, role_at_least


def _principal(role: str) -> Principal:
    return Principal(admin_id=uuid.uuid4(), workspace_id=uuid.uuid4(), role=role)


@pytest.mark.parametrize(
    ("role", "minimum", "ok"),
    [
        (Role.OWNER, Role.ADMIN, True),
        (Role.ADMIN, Role.ADMIN, True),
        (Role.AGENT, Role.ADMIN, False),
        (Role.RESTRICTED, Role.AGENT, False),
        (Role.AGENT, Role.AGENT, True),
        (Role.OWNER, Role.OWNER, True),
        (Role.ADMIN, Role.OWNER, False),
    ],
)
def test_role_at_least(role: str, minimum: str, ok: bool) -> None:
    assert role_at_least(role, minimum) is ok


def test_authorize_allows_and_denies() -> None:
    authorize(_principal(Role.OWNER), min_role=Role.ADMIN)  # no raise
    with pytest.raises(PermissionDeniedError):
        authorize(_principal(Role.AGENT), min_role=Role.ADMIN)


def test_unknown_role_is_denied() -> None:
    with pytest.raises(PermissionDeniedError):
        authorize(_principal("intruder"), min_role=Role.RESTRICTED)
