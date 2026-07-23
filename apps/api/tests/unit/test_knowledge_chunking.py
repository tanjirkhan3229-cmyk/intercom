"""Unit tests for heading-aware semantic chunking (P1.1, RFC-003 §4)."""

from __future__ import annotations

from relay.modules.knowledge.chunking import (
    MAX_TOKENS,
    chunk_article_body,
    chunk_segments,
    estimate_tokens,
    segments_from_blocks,
    segments_from_text,
)


def _long_text(heading: str, n_sentences: int) -> str:
    body = " ".join(
        f"Sentence {i} about the {heading} topic with several descriptive words here."
        for i in range(n_sentences)
    )
    return f"# {heading}\n\n{body}"


def test_segments_from_text_parses_markdown_headings() -> None:
    text = "# Billing\n\nPara one.\n\n## Refunds\n\nPara two."
    segments = segments_from_text(text)
    assert [s.heading_path for s in segments] == [("Billing",), ("Billing", "Refunds")]
    assert segments[1].text == "Para two."


def test_segments_from_blocks_tracks_headings() -> None:
    body = {
        "blocks": [
            {"type": "heading", "level": 1, "text": "Getting Started"},
            {"type": "paragraph", "text": "Welcome to the product."},
            {"type": "heading", "level": 2, "text": "Setup"},
            {"type": "paragraph", "text": "Install the widget."},
        ]
    }
    segments = segments_from_blocks(body)
    assert segments[0].heading_path == ("Getting Started",)
    assert segments[1].heading_path == ("Getting Started", "Setup")


def test_chunks_respect_max_and_have_overlap() -> None:
    chunks = chunk_segments(segments_from_text(_long_text("delivery", 120)))
    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= MAX_TOKENS
        assert c.heading_path == "delivery"
    # Adjacent chunks share a sentence-boundary overlap.
    first_words = set(chunks[0].content.split())
    second_words = set(chunks[1].content.split())
    assert first_words & second_words


def test_chunk_indices_are_contiguous() -> None:
    chunks = chunk_segments(segments_from_text(_long_text("invoices", 200)))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_oversize_sentence_is_split() -> None:
    giant = "word " * (MAX_TOKENS * 2)
    chunks = chunk_segments(segments_from_text(giant))
    assert chunks
    for c in chunks:
        assert c.token_count <= MAX_TOKENS


def test_estimate_tokens_counts_words_and_punctuation() -> None:
    assert estimate_tokens("hello, world!") == 4  # hello , world !


def test_empty_body_yields_no_chunks() -> None:
    assert chunk_article_body({"blocks": []}) == []
