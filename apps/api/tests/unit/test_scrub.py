"""PII scrubbing unit tests (P0.12, RFC-001 §10).

Proves the log/Sentry scrubber redacts secrets by key and emails embedded in strings, walks
nested structures, and preserves benign fields.
"""

from __future__ import annotations

from relay.core.observability.scrub import (
    REDACTED,
    scrub,
    scrub_processor,
    sentry_before_send,
)


def test_redacts_sensitive_keys() -> None:
    out = scrub({"Authorization": "Bearer abc", "password": "p", "api_key": "k", "safe": "ok"})
    assert out["Authorization"] == REDACTED
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["safe"] == "ok"


def test_redacts_emails_in_free_text() -> None:
    out = scrub({"event": "identify for jane.doe@example.com now"})
    assert "jane.doe@example.com" not in out["event"]
    assert REDACTED in out["event"]


def test_walks_nested_structures() -> None:
    out = scrub({"user": {"user_hash": "h", "notes": ["reach a@b.com", "plain"]}})
    assert out["user"]["user_hash"] == REDACTED
    assert REDACTED in out["user"]["notes"][0]
    assert out["user"]["notes"][1] == "plain"


def test_scrub_processor_returns_dict_and_redacts() -> None:
    event = scrub_processor(None, "info", {"event": "login x@y.com", "token": "t", "n": 3})
    assert isinstance(event, dict)
    assert event["token"] == REDACTED
    assert "x@y.com" not in event["event"]
    assert event["n"] == 3  # non-strings preserved


def test_sentry_before_send_scrubs_headers() -> None:
    event = sentry_before_send({"request": {"headers": {"Cookie": "s=1", "Accept": "json"}}}, {})
    assert event["request"]["headers"]["Cookie"] == REDACTED
    assert event["request"]["headers"]["Accept"] == "json"


def test_redacts_secrets_in_free_text_under_innocuous_keys() -> None:
    # Bearer tokens / JWTs / relaysk_ keys leak in exception messages + f-string logs; mask them
    # even when the KEY is innocuous (the value-level layer).
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc-DEF_123"
    out = scrub(
        {
            "event": f"auth failed: Authorization: Bearer {jwt}",
            "detail": "using relaysk_wrk_123_deadbeefcafe to call api",
            "raw_jwt": jwt,
        }
    )
    assert "eyJ" not in out["event"] and REDACTED in out["event"]
    assert "relaysk_wrk_123_deadbeefcafe" not in out["detail"] and REDACTED in out["detail"]
    assert out["raw_jwt"] == REDACTED  # bare JWT in a value


def test_traverses_sets_and_bytes() -> None:
    out = scrub({"tags": {"a@b.com", "plain"}, "body": b"reach jane@example.com"})
    assert isinstance(out["tags"], list)  # sets serialize to lists (hashable-safe)
    assert REDACTED in out["tags"] and "plain" in out["tags"]  # email masked, benign kept
    assert out["body"] == "reach ***"  # bytes decoded + email masked


def test_nested_frozenset_does_not_crash() -> None:
    # Regression: a scrubbed nested frozenset is unhashable — must not raise inside a comprehension.
    out = scrub({frozenset({"a@b.com"}), frozenset({"x"})})
    assert isinstance(out, list)
    assert all(isinstance(inner, list) for inner in out)


def test_key_matching_spares_benign_metric_fields() -> None:
    # Over-redaction fix: *_count metrics must survive while real *_token/*_session keys redact.
    out = scrub(
        {
            "token_count": 42,
            "session_count": 7,
            "access_token": "abc",
            "refresh_token": "def",
            "user_session": "ghi",
            "x-api-key": "k",
        }
    )
    assert out["token_count"] == 42
    assert out["session_count"] == 7
    assert out["access_token"] == REDACTED
    assert out["refresh_token"] == REDACTED
    assert out["user_session"] == REDACTED
    assert out["x-api-key"] == REDACTED
