"""Unit tests for external-source sync (P1.1) with injected fetch / PDF / OCR fakes."""

from __future__ import annotations

from relay.modules.knowledge.sources import (
    FetchResult,
    NullOcrEngine,
    sync_pdf_source,
    sync_snippet_source,
    sync_url_source,
)

_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/a</loc></url>
  <url><loc>https://ex.com/b</loc></url>
</urlset>"""


def _page(title: str) -> str:
    return (
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
        f"<p>Content for {title} page here.</p></main></body></html>"
    )


class DictFetcher:
    """Maps URLs to canned FetchResults; missing URLs return None (transport failure)."""

    def __init__(self, pages: dict[str, FetchResult]) -> None:
        self._pages = pages

    async def fetch(self, url: str) -> FetchResult | None:
        return self._pages.get(url)


async def test_url_sync_is_sitemap_aware() -> None:
    fetcher = DictFetcher(
        {
            "https://ex.com/sitemap.xml": FetchResult(
                "https://ex.com/sitemap.xml", 200, "application/xml", _SITEMAP
            ),
            "https://ex.com/a": FetchResult("https://ex.com/a", 200, "text/html", _page("Alpha")),
            "https://ex.com/b": FetchResult("https://ex.com/b", 200, "text/html", _page("Bravo")),
        }
    )
    docs = await sync_url_source({"url": "https://ex.com/"}, fetcher, max_pages=10)
    assert [d.title for d in docs] == ["Alpha", "Bravo"]
    assert "Content for Alpha" in docs[0].text


async def test_url_sync_single_page_without_sitemap() -> None:
    fetcher = DictFetcher(
        {"https://ex.com/faq": FetchResult("https://ex.com/faq", 200, "text/html", _page("FAQ"))}
    )
    docs = await sync_url_source(
        {"url": "https://ex.com/faq", "sitemap": False}, fetcher, max_pages=10
    )
    assert len(docs) == 1 and docs[0].title == "FAQ"


async def test_url_sync_respects_max_pages() -> None:
    fetcher = DictFetcher(
        {
            "https://ex.com/sitemap.xml": FetchResult(
                "https://ex.com/sitemap.xml", 200, "application/xml", _SITEMAP
            ),
            "https://ex.com/a": FetchResult("https://ex.com/a", 200, "text/html", _page("Alpha")),
            "https://ex.com/b": FetchResult("https://ex.com/b", 200, "text/html", _page("Bravo")),
        }
    )
    docs = await sync_url_source({"url": "https://ex.com/"}, fetcher, max_pages=1)
    assert len(docs) == 1


def test_snippet_source() -> None:
    docs = sync_snippet_source({"body": "Our office hours are 9-5."}, title="Hours")
    assert len(docs) == 1 and docs[0].text == "Our office hours are 9-5."
    assert sync_snippet_source({"body": "  "}, title="Empty") == []


class _FakePdf:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract(self, data: bytes) -> str:
        return self._text


class _FakeOcr:
    def extract(self, data: bytes) -> str:
        return "text recovered by OCR from a scanned page"


def test_pdf_text_extraction() -> None:
    docs = sync_pdf_source(
        b"%PDF", title="Manual", extractor=_FakePdf("Chapter 1. Setup guide."), ocr=NullOcrEngine()
    )
    assert len(docs) == 1 and "Setup guide" in docs[0].text


def test_pdf_ocr_fallback_when_no_text() -> None:
    docs = sync_pdf_source(b"%PDF", title="Scan", extractor=_FakePdf(""), ocr=_FakeOcr())
    assert len(docs) == 1 and "OCR" in docs[0].text


def test_pdf_empty_when_no_text_and_no_ocr() -> None:
    assert sync_pdf_source(b"%PDF", title="Scan", extractor=_FakePdf(""), ocr=NullOcrEngine()) == []
