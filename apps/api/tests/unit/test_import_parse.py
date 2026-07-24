"""Unit tests for the CSV import parse/validate/coerce layer (no DB/S3)."""

from __future__ import annotations

import pytest

from relay.modules.crm.import_worker import (
    _coerce_custom,
    parse_row,
    resolve_mapping,
)

_ATTRS = {"tier": "string", "score": "number", "vip": "boolean", "signup": "date", "tags": "list"}


def test_resolve_mapping_valid() -> None:
    resolved = resolve_mapping(
        {"Email": "email", "Ext": "external_id", "Tier": "custom.tier"}, _ATTRS
    )
    assert {r.header for r in resolved} == {"Email", "Ext", "Tier"}
    assert next(r for r in resolved if r.header == "Tier").is_custom


@pytest.mark.parametrize(
    "mapping",
    [
        {"Name": "name"},  # no identity key mapped
        {"X": "bogus"},  # unknown target
        {"T": "custom.undefined"},  # undefined custom attribute
        {"E": "email", "T": "custom.a.b"},  # nested custom key
    ],
)
def test_resolve_mapping_rejects(mapping: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        resolve_mapping(mapping, _ATTRS)


@pytest.mark.parametrize(
    ("data_type", "raw", "expected"),
    [
        ("string", "hi", "hi"),
        ("number", "5", 5),
        ("number", "5.5", 5.5),
        ("boolean", "true", True),
        ("boolean", "0", False),
        ("date", "2026-01-02", "2026-01-02"),
        ("list", "a, b ,c", ["a", "b", "c"]),
    ],
)
def test_coerce_custom_ok(data_type: str, raw: str, expected: object) -> None:
    assert _coerce_custom(data_type, raw) == expected


@pytest.mark.parametrize(
    ("data_type", "raw"),
    [("number", "nope"), ("boolean", "maybe"), ("date", "not-a-date")],
)
def test_coerce_custom_rejects(data_type: str, raw: str) -> None:
    with pytest.raises(ValueError):
        _coerce_custom(data_type, raw)


def test_parse_row_maps_core_and_custom() -> None:
    resolved = resolve_mapping(
        {"Ext": "external_id", "Email": "email", "Tier": "custom.tier", "Score": "custom.score"},
        _ATTRS,
    )
    row, err = parse_row(
        {"Ext": "u1", "Email": "a@b.com", "Tier": "pro", "Score": "42"}, resolved, _ATTRS, 1
    )
    assert err is None and row is not None
    assert row.external_id == "u1"
    assert row.email == "a@b.com"
    assert row.custom == {"tier": "pro", "score": 42}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"Ext": "", "Email": "not-an-email"}, "invalid_email"),
        ({"Ext": "", "Email": ""}, "missing_identity"),
        ({"Ext": "u1", "Email": "", "Kind": "robot"}, "invalid_kind"),
        ({"Ext": "u1", "Email": "", "Score": "abc"}, "invalid_custom"),
    ],
)
def test_parse_row_errors(raw: dict[str, str], code: str) -> None:
    resolved = resolve_mapping(
        {
            "Ext": "external_id",
            "Email": "email",
            "Kind": "kind",
            "Score": "custom.score",
        },
        _ATTRS,
    )
    row, err = parse_row(raw, resolved, _ATTRS, 7)
    assert row is None and err is not None
    assert err[0] == 7 and err[1] == code


def test_parse_row_empty_cells_are_unset() -> None:
    resolved = resolve_mapping({"Ext": "external_id", "Phone": "phone"}, _ATTRS)
    row, err = parse_row({"Ext": "u1", "Phone": ""}, resolved, _ATTRS, 1)
    assert err is None and row is not None
    assert row.phone is None
