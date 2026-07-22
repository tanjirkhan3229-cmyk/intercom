"""Unit tests for the custom-attribute type validator (RFC-002 §12). No DB required."""

from __future__ import annotations

from relay.modules.crm.service import _type_ok


def test_string() -> None:
    assert _type_ok("string", "hello")
    assert not _type_ok("string", 5)


def test_number_excludes_bool() -> None:
    assert _type_ok("number", 5)
    assert _type_ok("number", 5.5)
    # bool is an int subclass but must NOT satisfy `number`.
    assert not _type_ok("number", True)
    assert not _type_ok("number", "5")


def test_boolean() -> None:
    assert _type_ok("boolean", True)
    assert not _type_ok("boolean", 1)


def test_list() -> None:
    assert _type_ok("list", [1, 2, 3])
    assert not _type_ok("list", {"a": 1})


def test_date_accepts_iso() -> None:
    assert _type_ok("date", "2026-07-23")
    assert _type_ok("date", "2026-07-23T10:00:00Z")
    assert _type_ok("date", "2026-07-23T10:00:00+00:00")
    assert not _type_ok("date", "not-a-date")
    assert not _type_ok("date", 20260723)


def test_null_always_allowed() -> None:
    # A null clears an attribute regardless of its declared type.
    for data_type in ("string", "number", "boolean", "date", "list"):
        assert _type_ok(data_type, None)


def test_unknown_type_rejected() -> None:
    assert not _type_ok("mystery", "x")
