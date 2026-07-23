"""Public-API allowlist + scope helpers (P0.11), unit-tested without Redis/DB."""

from __future__ import annotations

from relay.core import public_api as pa


def test_allowlist_permits_public_resources() -> None:
    allowed = [
        ("GET", "/v0/contacts"),
        ("POST", "/v0/contacts"),
        ("POST", "/v0/contacts/identify"),
        ("GET", "/v0/contacts/usr_abc"),
        ("PATCH", "/v0/contacts/usr_abc"),
        ("DELETE", "/v0/contacts/usr_abc"),
        ("GET", "/v0/contacts/usr_abc/events"),
        ("POST", "/v0/events/track"),
        ("GET", "/v0/conversations"),
        ("POST", "/v0/conversations"),
        ("GET", "/v0/conversations/cnv_abc"),
        ("POST", "/v0/conversations/cnv_abc/reply"),
        ("GET", "/v0/articles"),
        ("GET", "/v0/articles/art_abc"),
    ]
    for method, path in allowed:
        assert pa._route_allowed(method, path), f"{method} {path} should be allowed"


def test_allowlist_denies_admin_and_unlisted_routes() -> None:
    denied = [
        ("GET", "/v0/api-keys"),
        ("POST", "/v0/api-keys"),
        ("GET", "/v0/members"),
        ("GET", "/v0/workspace"),
        ("GET", "/v0/teams"),
        ("POST", "/v0/webhook_subscriptions"),
        ("DELETE", "/v0/conversations/cnv_abc"),  # deleting a conversation isn't exposed
        ("POST", "/v0/conversations/cnv_abc/notes"),  # private notes aren't exposed
        ("POST", "/v0/conversations/cnv_abc/state"),
        ("GET", "/v0/saved-replies"),
        ("POST", "/v0/articles"),  # writing articles isn't exposed (read-only)
        ("GET", "/v0/hc/some-slug"),  # public help center is its own unauth surface
    ]
    for method, path in denied:
        assert not pa._route_allowed(method, path), f"{method} {path} should be denied"


def test_required_scope_by_method() -> None:
    assert pa._required_scope("GET") == "read"
    assert pa._required_scope("HEAD") == "read"
    assert pa._required_scope("POST") == "write"
    assert pa._required_scope("PATCH") == "write"
    assert pa._required_scope("DELETE") == "write"


def test_scope_write_implies_read() -> None:
    assert pa._has_scope(("read",), "read")
    assert pa._has_scope(("write",), "read")  # write implies read
    assert pa._has_scope(("write",), "write")
    assert pa._has_scope(("read", "write"), "write")
    assert not pa._has_scope(("read",), "write")
    assert not pa._has_scope((), "read")
    assert not pa._has_scope((), "write")
