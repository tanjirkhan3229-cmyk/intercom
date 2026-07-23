"""MIME parsing (inbound) and rendering (outbound) for the email channel (RFC-001 §6.6).

Inbound: parse SES-written raw MIME into a normalized ``ParsedEmail`` — addresses, subject, the
threading headers (Message-ID / In-Reply-To / References), a plain-text body (HTML down-converted),
and decoded attachments. Outbound: render an agent reply into raw MIME with correct threading
headers and a Reply-To carrying the stateless reply token.

Uses only the stdlib ``email`` package (modern ``policy.default``) — no third-party dependency.
"""

from __future__ import annotations

import email.policy
import email.utils
from dataclasses import dataclass, field
from email.message import EmailMessage, MIMEPart
from email.parser import BytesParser
from html.parser import HTMLParser


@dataclass
class Attachment:
    filename: str
    content_type: str
    content: bytes


@dataclass
class ParsedEmail:
    from_addr: str
    from_name: str
    to_addrs: list[str]
    subject: str
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    text: str
    attachments: list[Attachment] = field(default_factory=list)


_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})


class _TextExtractor(HTMLParser):
    """Minimal, dependency-free HTML→text: drops script/style, breaks on block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        # Collapse runs of blank lines the block-tag breaks introduce.
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _strip_message_id(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    return v or None


def _safe_body_text(body: MIMEPart) -> str:
    """Extract a part's text, tolerating unknown charsets / bad transfer-encodings.

    ``get_content()`` raises ``LookupError`` on an unknown/misspelled charset (fully
    sender-controlled) and can raise ``UnicodeDecodeError``/``ValueError`` on malformed bytes.
    parse() promises never to raise on odd mail, so we fall back to a lossy raw decode."""
    try:
        content = body.get_content()
        content_str = content if isinstance(content, str) else str(content)
    except (LookupError, UnicodeDecodeError, ValueError):
        payload = body.get_payload(decode=True)
        raw_text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
        return raw_text.strip()
    if body.get_content_subtype() == "html":
        return html_to_text(content_str)
    return content_str.strip()


def parse(raw: bytes) -> ParsedEmail:
    """Parse raw MIME bytes into a ``ParsedEmail`` (never raises on odd-but-valid mail)."""
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)

    from_name, from_addr = email.utils.parseaddr(str(msg.get("From", "")))
    to_headers: list[str] = []
    for header in ("To", "Delivered-To", "Cc"):
        to_headers.extend(msg.get_all(header, []))
    to_addrs = [addr for _name, addr in email.utils.getaddresses(to_headers) if addr]

    references_raw = str(msg.get("References", "") or "")
    references = [r for r in references_raw.replace(",", " ").split() if r]

    text = ""
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = _safe_body_text(body)

    attachments: list[Attachment] = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_payload(decode=True)
        except (LookupError, ValueError, TypeError):
            continue  # unknown encoding / malformed part — skip the attachment, never crash
        if not isinstance(payload, bytes):
            continue
        attachments.append(
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type(),
                content=payload,
            )
        )

    return ParsedEmail(
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=to_addrs,
        subject=str(msg.get("Subject", "") or ""),
        message_id=_strip_message_id(msg.get("Message-ID")),
        in_reply_to=_strip_message_id(msg.get("In-Reply-To")),
        references=references,
        text=text,
        attachments=attachments,
    )


def make_message_id(domain: str) -> str:
    """Generate a random RFC-822 Message-ID (``<...@domain>``)."""
    return email.utils.make_msgid(domain=domain)


def deterministic_message_id(token: str, domain: str) -> str:
    """A stable RFC-822 Message-ID for an outbound email, keyed by ``token`` (e.g. the part id).

    Re-rendering the same part yields the same Message-ID, so an at-least-once retry that
    re-sends after a crash-before-commit is de-duplicable by the receiving MTA/client."""
    return f"<em-{token}@{domain}>"


def build_outbound(
    *,
    sender: str,
    sender_name: str,
    to_addr: str,
    reply_to: str,
    subject: str,
    text_body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> EmailMessage:
    """Render an agent reply into a threaded ``EmailMessage`` (plain text + minimal HTML)."""
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((sender_name, sender))
    msg["To"] = to_addr
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = list(references or [])
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if refs:
        msg["References"] = " ".join(refs)

    msg.set_content(text_body)
    # Minimal HTML alternative (paragraphs preserved). No inline eval / remote content.
    html = (
        "<html><body>"
        + "".join(f"<p>{_escape(line)}</p>" for line in text_body.splitlines() if line.strip())
        + "</body></html>"
    )
    msg.add_alternative(html, subtype="html")
    return msg


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_bytes(msg: EmailMessage) -> bytes:
    return msg.as_bytes()


def reply_subject(original: str | None) -> str:
    """Prefix ``Re:`` once for a reply subject."""
    subject = (original or "").strip()
    if not subject:
        return "Re:"
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"
