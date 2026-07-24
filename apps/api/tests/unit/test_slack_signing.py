"""Unit tests for Slack request-signature verification (no DB)."""

from __future__ import annotations

from relay.modules.integrations import slack_sign

SECRET = "8f742231b10c8538a055a3ee6ed7a9d5"
BODY = b'{"type":"event_callback","team_id":"T1"}'
TS = 1_700_000_000


def test_roundtrip_verifies() -> None:
    sig = slack_sign.compute_signature(SECRET, TS, BODY)
    assert slack_sign.verify_signature(
        SECRET, timestamp=TS, body=BODY, header=sig, tolerance_seconds=300, now=TS + 10
    )


def test_tampered_body_fails() -> None:
    sig = slack_sign.compute_signature(SECRET, TS, BODY)
    assert not slack_sign.verify_signature(
        SECRET, timestamp=TS, body=BODY + b"x", header=sig, tolerance_seconds=300, now=TS
    )


def test_wrong_secret_fails() -> None:
    sig = slack_sign.compute_signature("other-secret", TS, BODY)
    assert not slack_sign.verify_signature(
        SECRET, timestamp=TS, body=BODY, header=sig, tolerance_seconds=300, now=TS
    )


def test_expired_timestamp_fails() -> None:
    sig = slack_sign.compute_signature(SECRET, TS, BODY)
    assert not slack_sign.verify_signature(
        SECRET, timestamp=TS, body=BODY, header=sig, tolerance_seconds=300, now=TS + 10_000
    )


def test_empty_header_fails() -> None:
    assert not slack_sign.verify_signature(
        SECRET, timestamp=TS, body=BODY, header="", tolerance_seconds=300, now=TS
    )
