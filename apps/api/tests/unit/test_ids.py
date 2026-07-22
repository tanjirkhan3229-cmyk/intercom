"""Unit tests for the UUIDv7 + base62 public-id helpers (RFC-002 §5.1)."""

from __future__ import annotations

import pytest

from relay.core.ids import (
    IdPrefix,
    decode_public_id,
    encode_public_id,
    uuid7,
)


def test_uuid7_is_version_7_and_variant_rfc4122() -> None:
    for _ in range(500):
        u = uuid7()
        assert u.version == 7
        assert (u.int >> 62) & 0b11 == 0b10  # variant 10xx


def test_uuid7_is_monotonic_and_unique() -> None:
    ids = [uuid7() for _ in range(2000)]
    assert ids == sorted(ids), "uuid7 must be time-ordered / monotonic"
    assert len(set(ids)) == len(ids), "uuid7 must be unique"


def test_public_id_roundtrip() -> None:
    u = uuid7()
    public = encode_public_id(IdPrefix.WORKSPACE, u)
    assert public.startswith("wrk_")
    assert decode_public_id(IdPrefix.WORKSPACE, public) == u


def test_public_id_prefix_mismatch_raises() -> None:
    public = encode_public_id(IdPrefix.WORKSPACE, uuid7())
    with pytest.raises(ValueError):
        decode_public_id(IdPrefix.ADMIN, public)
