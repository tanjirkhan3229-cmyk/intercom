"""External source sync (P1.1): URL crawl, PDF ingestion, snippets → normalised documents.

Each ``sync_*`` turns an :class:`~relay.modules.knowledge.models.ExternalSource` config into a
list of :class:`SourceDocument` (a page/file → title + clean text) that the indexer chunks and
embeds. All I/O is behind injectable protocols (``Fetcher``, ``PdfExtractor``, ``OcrEngine``) so
the sync logic is unit-tested with fakes and never touches the network in CI (RFC-001 §9: every
external call bounded + timed out).

- URL: sitemap-aware crawl (fetch ``/sitemap.xml`` or a configured sitemap, else the single page),
  boilerplate stripped via :mod:`readability`, capped at ``max_pages``.
- PDF: text extraction (pypdf) with an OCR fallback when a scanned PDF yields no text.
- Snippet: the admin-authored body, verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

from relay.core.logging import get_logger
from relay.modules.knowledge.readability import extract_main_text, parse_sitemap

log = get_logger(__name__)

# Below this many extracted characters a PDF is treated as scanned/empty → OCR fallback.
_PDF_TEXT_MIN_CHARS = 32


@dataclass(frozen=True)
class SourceDocument:
    """One ingested unit (a crawled page, a PDF, a snippet): stable key + title + clean text."""

    key: str
    title: str
    text: str


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult | None:
        """Fetch a URL. Returns ``None`` on transport failure (never raises to the caller)."""
        ...


@runtime_checkable
class PdfExtractor(Protocol):
    def extract(self, data: bytes) -> str: ...


@runtime_checkable
class OcrEngine(Protocol):
    def extract(self, data: bytes) -> str: ...


class HttpFetcher:
    """Prod URL fetcher: bounded timeout + response-size cap (RFC-001 §9)."""

    def __init__(self, *, timeout_seconds: float, max_bytes: int) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes

    async def fetch(self, url: str) -> FetchResult | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "RelayKnowledgeBot/1.0"})
        except httpx.HTTPError as exc:
            log.warning("knowledge.source.fetch_failed", url=url, error=str(exc))
            return None
        body = resp.content[: self._max_bytes]
        ctype = resp.headers.get("content-type", "")
        return FetchResult(
            url=str(resp.url),
            status=resp.status_code,
            content_type=ctype,
            text=body.decode(resp.encoding or "utf-8", errors="replace"),
        )


class PypdfExtractor:
    """Prod PDF text extraction via pypdf (pure-python)."""

    def extract(self, data: bytes) -> str:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)


class NullOcrEngine:
    """Default OCR: unavailable. Prod wires a Tesseract-backed engine (heavy binary; not in CI)."""

    def extract(self, data: bytes) -> str:
        log.info("knowledge.source.ocr_unavailable", bytes=len(data))
        return ""


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


async def sync_url_source(
    config: dict[str, Any],
    fetcher: Fetcher,
    *,
    max_pages: int,
) -> list[SourceDocument]:
    """Crawl a URL source. Sitemap-first, then the discovered pages (same host), capped."""
    start = config.get("url")
    if not start:
        return []
    urls: list[str] = []
    if config.get("sitemap", True):
        sitemap_url = config.get("sitemap_url") or urljoin(start, "/sitemap.xml")
        res = await fetcher.fetch(sitemap_url)
        if res and res.ok and ("xml" in res.content_type or "<urlset" in res.text[:200].lower()):
            urls = [u for u in parse_sitemap(res.text) if _same_host(u, start)]
    if not urls:
        urls = [start]

    docs: list[SourceDocument] = []
    seen: set[str] = set()
    for url in urls:
        if len(docs) >= max_pages:
            break
        if url in seen:
            continue
        seen.add(url)
        res = await fetcher.fetch(url)
        if not res or not res.ok:
            continue
        if "html" not in res.content_type and res.content_type:
            continue
        title, text = extract_main_text(res.text)
        if text.strip():
            docs.append(SourceDocument(key=url, title=title or url, text=text))
    return docs


def sync_pdf_source(
    data: bytes,
    *,
    title: str,
    extractor: PdfExtractor,
    ocr: OcrEngine,
) -> list[SourceDocument]:
    """Extract a PDF's text; fall back to OCR when extraction yields (near) nothing."""
    text = ""
    try:
        text = extractor.extract(data)
    except Exception as exc:  # malformed PDF must not poison the pipeline (RFC-001 §9)
        log.warning("knowledge.source.pdf_extract_failed", title=title, error=str(exc))
    if len(text.strip()) < _PDF_TEXT_MIN_CHARS:
        ocr_text = ocr.extract(data)
        if ocr_text.strip():
            text = ocr_text
    if not text.strip():
        return []
    return [SourceDocument(key=title, title=title, text=text)]


def sync_snippet_source(config: dict[str, Any], *, title: str) -> list[SourceDocument]:
    """A snippet is its own body — the admin's curated repair text (RFC-003 §4 custom answers)."""
    body = str(config.get("body", "")).strip()
    if not body:
        return []
    return [SourceDocument(key=title, title=title, text=body)]
