"""Unit tests for the stdlib HTML readability + sitemap parsing (P1.1 URL crawler)."""

from __future__ import annotations

from relay.modules.knowledge.readability import extract_main_text, parse_sitemap

_PAGE = """
<html>
  <head><title>Refund Policy</title></head>
  <body>
    <nav>Home Pricing Login</nav>
    <header>Site header junk</header>
    <main>
      <h1>Refund Policy</h1>
      <p>You can request a refund within 30 days.</p>
      <h2>How to request</h2>
      <p>Open billing settings and click refund.</p>
      <script>trackAnalytics()</script>
    </main>
    <footer>Copyright 2026</footer>
  </body>
</html>
"""


def test_extracts_main_and_strips_boilerplate() -> None:
    title, text = extract_main_text(_PAGE)
    assert title == "Refund Policy"
    assert "# Refund Policy" in text
    assert "## How to request" in text
    assert "within 30 days" in text
    # Boilerplate + scripts are gone.
    assert "Pricing Login" not in text
    assert "Copyright 2026" not in text
    assert "trackAnalytics" not in text


def test_falls_back_to_body_without_main() -> None:
    html = "<html><body><h1>Title</h1><p>Body text here.</p></body></html>"
    _title, text = extract_main_text(html)
    assert "# Title" in text
    assert "Body text here." in text


def test_parse_sitemap_extracts_locs_namespace_tolerant() -> None:
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/a</loc></url>
      <url><loc>https://ex.com/b</loc></url>
    </urlset>"""
    assert parse_sitemap(xml) == ["https://ex.com/a", "https://ex.com/b"]


def test_parse_sitemap_bad_xml_is_empty() -> None:
    assert parse_sitemap("not xml <<<") == []
