"""API-key parsing (P0.11)."""

from __future__ import annotations

import uuid

import pytest

from relay.core.api_key import API_KEY_LABEL, looks_like_api_key, parse_api_key
from relay.core.ids import IdPrefix, encode_public_id


def test_parse_roundtrip_recovers_workspace() -> None:
    ws = uuid.uuid4()
    # Secret deliberately contains '_' and '-' (token_urlsafe can) to prove structural parsing.
    key = f"{API_KEY_LABEL}_{encode_public_id(IdPrefix.WORKSPACE, ws)}_abc_def-xyz123"
    assert parse_api_key(key) == ws


def test_looks_like_api_key() -> None:
    assert looks_like_api_key("relaysk_wrk_x_y")
    assert not looks_like_api_key("eyJhbGci.jwt.token")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "relaysk_",
        "relaysk_wrk_",
        "relaysk_notaprefix_x_y",
        "nope_wrk_x_y",
        "relaysk_wrk_$$$_secret",  # invalid base62 in the workspace body
    ],
)
def test_malformed_raises_valueerror(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_api_key(bad)
