"""Role-based access control — the single choke point (RFC-001 §10).

Every permission decision in every module goes through :func:`authorize`. Keeping it in one
kernel function means the policy is auditable and can't drift across scattered
``if role == ...`` checks. Lives in ``relay.core`` so all module services can call it.
"""

from __future__ import annotations

from relay.core.errors import PermissionDeniedError
from relay.core.principal import Principal


class Role:
    OWNER = "owner"
    ADMIN = "admin"
    AGENT = "agent"
    RESTRICTED = "restricted"


# Higher number = more privilege.
ROLE_RANK: dict[str, int] = {
    Role.RESTRICTED: 0,
    Role.AGENT: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, 999)


def authorize(principal: Principal, *, min_role: str) -> None:
    """Raise :class:`PermissionDeniedError` unless ``principal`` meets ``min_role``.

    The one function services call: ``authorize(principal, min_role=Role.ADMIN)``.
    """
    if not role_at_least(principal.role, min_role):
        raise PermissionDeniedError(
            f"requires role '{min_role}' or higher",
            details={"role": principal.role, "required": min_role},
        )
