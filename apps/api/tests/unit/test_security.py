"""Unit tests for password hashing, secret hashing, and access-token JWTs."""

from __future__ import annotations

import uuid

import jwt
import pytest

from relay.core.security import (
    compare_secret,
    create_access_token,
    decode_access_token,
    generate_secret,
    hash_password,
    hash_secret,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_verify_password_handles_garbage_hash() -> None:
    assert verify_password("x", "not-a-hash") is False


def test_secret_hash_is_stable_and_comparable() -> None:
    s = generate_secret()
    assert hash_secret(s) == hash_secret(s)
    assert compare_secret(s, hash_secret(s))
    assert not compare_secret(s, hash_secret(generate_secret()))


def test_access_token_roundtrip() -> None:
    admin_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    token = create_access_token(admin_id=admin_id, workspace_id=ws_id, role="owner")
    claims = decode_access_token(token)
    assert claims["sub"] == str(admin_id)
    assert claims["ws"] == str(ws_id)
    assert claims["role"] == "owner"
    assert claims["type"] == "access"


def test_access_token_rejects_tampering() -> None:
    token = create_access_token(admin_id=uuid.uuid4(), workspace_id=uuid.uuid4(), role="agent")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "tamper")
