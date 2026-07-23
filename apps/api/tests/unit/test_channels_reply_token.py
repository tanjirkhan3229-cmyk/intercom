"""Unit tests for stateless plus-addressed reply tokens (P0.7, RFC-001 §6.6)."""

from __future__ import annotations

import uuid

from relay.modules.channels import reply_token


def test_roundtrip() -> None:
    ws, conv = uuid.uuid4(), uuid.uuid4()
    token = reply_token.make_reply_token(ws, conv)
    assert reply_token.parse_reply_token(token) == (ws, conv)


def test_reply_address_shape() -> None:
    ws, conv = uuid.uuid4(), uuid.uuid4()
    addr = reply_token.reply_address(reply_token.make_reply_token(ws, conv))
    assert addr.startswith("reply+")
    assert "@" in addr


def test_tamper_is_rejected() -> None:
    ws, conv = uuid.uuid4(), uuid.uuid4()
    token = reply_token.make_reply_token(ws, conv)
    # Flip a character in the middle → payload or signature no longer matches.
    idx = len(token) // 2
    mutated = token[:idx] + ("A" if token[idx] != "A" else "B") + token[idx + 1 :]
    assert reply_token.parse_reply_token(mutated) != (ws, conv)


def test_garbage_returns_none() -> None:
    assert reply_token.parse_reply_token("not-a-token!!") is None
    assert reply_token.parse_reply_token("") is None


def test_wrong_key_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ws, conv = uuid.uuid4(), uuid.uuid4()
    token = reply_token.make_reply_token(ws, conv)
    from relay.settings import get_settings

    monkeypatch.setattr(get_settings(), "email_reply_token_secret", "a-different-secret-key-xyz")
    assert reply_token.parse_reply_token(token) is None
