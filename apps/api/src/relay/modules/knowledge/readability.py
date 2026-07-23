"""Lightweight, dependency-free HTML readability + sitemap parsing for the URL crawler (P1.1).

"Readability extraction" here = strip boilerplate (nav/header/footer/aside/script/style/forms),
prefer a ``<main>``/``<article>`` region when present, and emit clean markdown-ish text (headings
as ``#`` lines, block elements as blank-line paragraphs) that ``chunking.segments_from_text``
understands. It is stdlib-only (``html.parser``) so CI is hermetic; prod can swap in trafilatura /
readability-lxml behind :func:`extract_main_text` without touching callers.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from xml.etree import ElementTree

_BOILERPLATE_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "button",
        "svg",
        "iframe",
        "template",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "li",
        "div",
        "section",
        "article",
        "main",
        "blockquote",
        "pre",
        "td",
        "th",
        "tr",
        "figcaption",
        "dd",
        "dt",
    }
)
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_WS_RE = re.compile(r"[ \t\f\v]+")
_MAIN_PRESENT_RE = re.compile(r"<(main|article)\b", re.IGNORECASE)


class _Extractor(HTMLParser):
    def __init__(self, *, restrict_to_main: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._restrict = restrict_to_main
        self._skip_depth = 0
        self._main_depth = 0
        self._title_parts: list[str] = []
        self._in_title = False
        self._heading_level: int | None = None
        self._buf: list[str] = []
        self.lines: list[str] = []

    # --- capture gating -------------------------------------------------------------------
    @property
    def _capturing(self) -> bool:
        if self._skip_depth > 0:
            return False
        if self._restrict:
            return self._main_depth > 0
        return True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _BOILERPLATE_TAGS:
            self._skip_depth += 1
            return
        if tag in ("main", "article"):
            self._main_depth += 1
        if tag in ("br",):
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._heading_level = _HEADING_TAGS[tag]
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _BOILERPLATE_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _HEADING_TAGS or tag in _BLOCK_TAGS:
            self._flush()
        if tag in ("main", "article"):
            self._main_depth = max(0, self._main_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if not self._capturing:
            return
        text = _WS_RE.sub(" ", data)
        if text.strip():
            self._buf.append(text)

    def _flush(self) -> None:
        if not self._buf:
            self._heading_level = None
            return
        text = _WS_RE.sub(" ", "".join(self._buf)).strip()
        self._buf.clear()
        if not text:
            self._heading_level = None
            return
        if self._heading_level is not None:
            self.lines.append("")
            self.lines.append(f"{'#' * self._heading_level} {text}")
            self.lines.append("")
            self._heading_level = None
        else:
            self.lines.append(text)
            self.lines.append("")

    @property
    def title(self) -> str:
        return _WS_RE.sub(" ", "".join(self._title_parts)).strip()


def extract_main_text(html: str) -> tuple[str, str]:
    """Return ``(title, markdown_text)`` for an HTML document, boilerplate stripped.

    Prefers a ``<main>``/``<article>`` region when the document has one; otherwise falls back to
    the whole body minus boilerplate tags.
    """
    restrict = bool(_MAIN_PRESENT_RE.search(html))
    parser = _Extractor(restrict_to_main=restrict)
    parser.feed(html)
    parser._flush()
    # Collapse runs of blank lines.
    out_lines: list[str] = []
    for line in parser.lines:
        if line == "" and (not out_lines or out_lines[-1] == ""):
            continue
        out_lines.append(line)
    text = "\n".join(out_lines).strip()
    return parser.title, text


def parse_sitemap(xml: str) -> list[str]:
    """Extract URLs from a ``<urlset>`` or ``<sitemapindex>`` sitemap (``<loc>`` entries).

    Namespace-tolerant. Returns locs in document order; a sitemap index returns its child sitemap
    URLs (the crawler fetches those next).
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    locs: list[str] = []
    for loc in root.iter():
        tag = loc.tag.rsplit("}", 1)[-1]  # strip namespace
        if tag == "loc" and loc.text and loc.text.strip():
            locs.append(loc.text.strip())
    return locs
