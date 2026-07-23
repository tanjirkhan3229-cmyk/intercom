"""Unit tests for the ``knowledge`` block helpers (no DB — pure functions)."""

from __future__ import annotations

from relay.modules.knowledge.blocks import blocks_to_text, excerpt, slugify


def test_blocks_to_text_extracts_paragraphs_and_headings() -> None:
    body = {
        "blocks": [
            {"type": "heading", "text": "Refund policy"},
            {"type": "paragraph", "text": "You can request a refund within 30 days."},
        ]
    }
    text = blocks_to_text(body)
    assert "Refund policy" in text
    assert "request a refund" in text


def test_blocks_to_text_handles_lists_code_and_images() -> None:
    body = {
        "blocks": [
            {"type": "list", "items": ["first step", "second step"]},
            {"type": "code", "code": "print('hello')"},
            {"type": "image", "url": "https://x/y.png", "alt": "a diagram"},
        ]
    }
    text = blocks_to_text(body)
    assert "first step" in text and "second step" in text
    assert "print('hello')" in text
    assert "a diagram" in text  # alt text indexed
    assert "https://x/y.png" not in text  # urls are not indexed


def test_blocks_to_text_recurses_inline_runs() -> None:
    body = {"blocks": [{"type": "paragraph", "content": [{"text": "hello "}, {"text": "world"}]}]}
    assert blocks_to_text(body) == "hello world"


def test_blocks_to_text_is_defensive_against_malformed_input() -> None:
    # Never raises — a weird body must not be able to 500 a publish.
    assert blocks_to_text(None) == ""
    assert blocks_to_text("just a string") == "just a string"
    assert blocks_to_text({}) == ""
    # A stray string under a container key is harmlessly indexed (defensive, never raises).
    assert blocks_to_text({"blocks": "not-a-list"}) == "not-a-list"
    assert blocks_to_text([1, 2, 3]) == ""
    assert blocks_to_text({"blocks": [{"type": "unknown"}]}) == ""


def test_blocks_to_text_collapses_whitespace() -> None:
    body = {"blocks": [{"type": "paragraph", "text": "  spaced   \n\n out  "}]}
    assert blocks_to_text(body) == "spaced out"


def test_slugify() -> None:
    assert slugify("Getting Started!") == "getting-started"
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert slugify("Héllo Wörld") == "h-llo-w-rld"
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"


def test_excerpt_truncates_on_word_boundary() -> None:
    assert excerpt("short text") == "short text"
    long = "word " * 100
    out = excerpt(long, limit=20)
    assert len(out) <= 21  # 20 + ellipsis, word-boundary trimmed
    assert out.endswith("…")


def test_excerpt_empty() -> None:
    assert excerpt("") == ""
