"""Server-side email template rendering (P1.8).

Renders a campaign template into an HTML + plain-text body pair with ``{{ variable }}``
substitution. Values are always HTML-escaped on substitution, so a value can never inject markup.

MJML fidelity: this is a **documented minimal subset** transformer (``mj-body/mj-section/mj-column/
mj-text/mj-button/mj-image/mj-divider/mj-raw``) sufficient for one-off broadcasts; a full MJML
compiler is a Node library and is a fast-follow (the drag-drop editor is a frontend deliverable). A
template with no ``<mjml>`` root is treated as raw HTML, so hand-written HTML also works.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# MJML tag → (open_html, close_html). Unknown mj-* tags collapse to a <div>.
_MJML_TAGS: dict[str, tuple[str, str]] = {
    "mjml": ("", ""),
    "mj-body": ('<div style="margin:0;padding:0">', "</div>"),
    "mj-section": ('<table role="presentation" width="100%"><tr>', "</tr></table>"),
    "mj-column": ("<td>", "</td>"),
    "mj-text": ("<div>", "</div>"),
    "mj-raw": ("", ""),
    "mj-divider": ("<hr/>", ""),
}


@dataclass(frozen=True)
class RenderedEmail:
    html: str
    text: str


def _flatten(context: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested context into ``{"contact.name": "...", "plan": "..."}`` string values."""
    flat: dict[str, str] = {}
    for key, value in context.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        elif value is not None:
            flat[path] = str(value)
    return flat


def substitute(template: str, context: dict[str, Any], *, escape: bool = True) -> str:
    """Replace ``{{ path }}`` with the context value (missing → empty string).

    ``escape=True`` (the default, for HTML bodies) HTML-escapes each value so it can never inject
    markup; ``escape=False`` is for plain-text targets like the subject line, where entity-encoding
    would corrupt legitimate ``&``/``<``/``>`` characters.
    """
    flat = _flatten(context)

    def _repl(m: re.Match[str]) -> str:
        value = flat.get(m.group(1), "")
        return html.escape(value) if escape else value

    return _VAR_RE.sub(_repl, template)


class _MjmlToHtml(HTMLParser):
    """Transform the supported MJML subset to table-based HTML; pass through plain HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "mj-button":
            href = next((v or "" for k, v in attrs if k == "href"), "")
            self.out.append(f'<a href="{html.escape(href)}">')
        elif tag == "mj-image":
            src = next((v or "" for k, v in attrs if k == "src"), "")
            self.out.append(f'<img src="{html.escape(src)}"/>')
        elif tag in _MJML_TAGS:
            self.out.append(_MJML_TAGS[tag][0])
        elif tag.startswith("mj-"):
            self.out.append("<div>")
        else:
            attr_str = "".join(f' {k}="{html.escape(v)}"' for k, v in attrs if v is not None)
            self.out.append(f"<{tag}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "mj-image":
            src = next((v or "" for k, v in attrs if k == "src"), "")
            self.out.append(f'<img src="{html.escape(src)}"/>')
        elif tag == "mj-divider":
            self.out.append("<hr/>")
        elif tag.startswith("mj-"):
            pass
        else:
            self.out.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "mj-button":
            self.out.append("</a>")
        elif tag in _MJML_TAGS:
            self.out.append(_MJML_TAGS[tag][1])
        elif tag.startswith("mj-"):
            self.out.append("</div>")
        else:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def _mjml_to_html(template: str) -> str:
    if "<mjml" not in template.lower():
        return template  # already HTML
    parser = _MjmlToHtml()
    parser.feed(template)
    parser.close()
    return "".join(parser.out)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "p", "div", "tr", "hr", "mj-text", "mj-section", "mj-divider"):
            self.parts.append("\n")


def _to_text(rendered_html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(rendered_html)
    extractor.close()
    text = "".join(extractor.parts)
    # Collapse runs of blank lines / trailing spaces.
    lines = [line.strip() for line in text.splitlines()]
    collapsed = "\n".join(line for line in lines if line)
    return collapsed.strip()


def render_email(*, template: str, context: dict[str, Any]) -> RenderedEmail:
    """Render a template + context into an ``(html, text)`` pair.

    Order matters: transform the MJML **first**, then substitute the (HTML-escaped) variable values
    into the generated HTML. Escaping-then-parsing would let the HTML parser decode ``&lt;`` back to
    ``<`` (``convert_charrefs``), re-exposing injected markup — so variables are substituted after
    the parse and are never re-parsed.
    """
    body_html = substitute(_mjml_to_html(template), context, escape=True)
    return RenderedEmail(html=body_html, text=_to_text(body_html))
