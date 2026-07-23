"""Webhook HMAC signing (P0.11) + the documented verifier (acceptance: doc snippet verified)."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from relay.modules.webhooks import signing

_DOC = (
    Path(__file__).resolve().parents[2].parent.parent
    / "docs"
    / "webhooks"
    / "signature-verification.md"
)


def _doc_verify(secret, payload, signature_header, timestamp_header, now, tolerance=300):  # type: ignore[no-untyped-def]
    """A standalone reimplementation kept byte-identical to the Python snippet in the doc.

    ``test_doc_documents_the_actual_algorithm`` asserts the doc still contains these exact lines,
    so if the doc's algorithm drifts from the signer this file fails to represent it and the
    parity test breaks — tying doc, this verifier, and ``signing`` together.
    """
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    signed = f"{timestamp}.".encode() + payload
    expected = "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def test_compute_and_verify_roundtrip() -> None:
    secret, body, ts = "shhh-secret", b'{"hello":"world"}', 1_753_272_000
    header = signing.compute_signature(secret, ts, body)
    assert header.startswith("v1=")
    assert signing.verify_signature(
        secret, timestamp=ts, body=body, header=header, tolerance_seconds=300, now=ts
    )


def test_verify_rejects_tamper() -> None:
    secret, body, ts = "s", b"payload", 1000
    header = signing.compute_signature(secret, ts, body)
    assert not signing.verify_signature(
        secret, timestamp=ts, body=b"payload!", header=header, tolerance_seconds=300, now=ts
    )
    assert not signing.verify_signature(
        "wrong-secret", timestamp=ts, body=body, header=header, tolerance_seconds=300, now=ts
    )


def test_verify_rejects_stale_timestamp() -> None:
    secret, body, ts = "s", b"p", 1000
    header = signing.compute_signature(secret, ts, body)
    assert not signing.verify_signature(
        secret, timestamp=ts, body=body, header=header, tolerance_seconds=300, now=ts + 301
    )


def test_doc_documents_the_actual_algorithm() -> None:
    """The published doc must state the same algorithm the signer uses (fails if either drifts)."""
    doc = _DOC.read_text()
    assert 'signed = f"{timestamp}.".encode() + payload' in doc
    assert '"v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()' in doc
    assert "hmac.compare_digest" in doc
    assert "Relay-Signature" in doc
    assert "Relay-Timestamp" in doc


def test_documented_verifier_matches_signer() -> None:
    """Acceptance: the documented verification snippet validates a real Relay signature."""
    secret, body, ts = "doc-secret", b'{"a":1}', 2_000_000
    header = signing.compute_signature(secret, ts, body)
    assert _doc_verify(secret, body, header, str(ts), ts) is True
    assert _doc_verify(secret, body + b"x", header, str(ts), ts) is False  # tamper
    assert _doc_verify(secret, body, header, str(ts), ts + 10_000) is False  # stale
    assert _doc_verify(secret, body, header, "not-an-int", ts) is False  # bad header
