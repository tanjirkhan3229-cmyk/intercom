"""Unit tests for MIME parsing/rendering (P0.7 email, RFC-001 §6.6)."""

from __future__ import annotations

from email.message import EmailMessage

from relay.modules.channels import mime


def _plain(**headers: str) -> bytes:
    msg = EmailMessage()
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content(headers.get("body", "Hello world"))
    return msg.as_bytes()


def test_parse_plain_headers_and_body() -> None:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "support@acme.test"
    msg["Subject"] = "Need help"
    msg["Message-ID"] = "<abc@example.com>"
    msg.set_content("Hello, my order is late.")

    parsed = mime.parse(msg.as_bytes())
    assert parsed.from_addr == "alice@example.com"
    assert parsed.from_name == "Alice"
    assert "support@acme.test" in parsed.to_addrs
    assert parsed.subject == "Need help"
    assert parsed.message_id == "<abc@example.com>"
    assert "order is late" in parsed.text


def test_parse_threading_headers() -> None:
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "support@acme.test"
    msg["Message-ID"] = "<m2@x.com>"
    msg["In-Reply-To"] = "<m1@acme.test>"
    msg["References"] = "<m0@acme.test> <m1@acme.test>"
    msg.set_content("reply")
    parsed = mime.parse(msg.as_bytes())
    assert parsed.in_reply_to == "<m1@acme.test>"
    assert parsed.references == ["<m0@acme.test>", "<m1@acme.test>"]


def test_missing_message_id_is_none() -> None:
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "support@acme.test"
    msg.set_content("no id")
    assert mime.parse(msg.as_bytes()).message_id is None


def test_html_body_downconverted() -> None:
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "support@acme.test"
    msg.set_content("<html><body><p>Hello</p><p>World</p></body></html>", subtype="html")
    parsed = mime.parse(msg.as_bytes())
    assert "Hello" in parsed.text
    assert "World" in parsed.text
    assert "<p>" not in parsed.text


def test_html_to_text_strips_script() -> None:
    text = mime.html_to_text("<p>Visible</p><script>alert('x')</script>")
    assert "Visible" in text
    assert "alert" not in text


def test_attachments_decoded() -> None:
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "support@acme.test"
    msg.set_content("see attached")
    msg.add_attachment(b"filedata", maintype="application", subtype="pdf", filename="doc.pdf")
    parsed = mime.parse(msg.as_bytes())
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "doc.pdf"
    assert parsed.attachments[0].content == b"filedata"


def test_bad_charset_body_does_not_raise() -> None:
    # A sender-declared unknown charset makes email.get_content() raise LookupError; parse() must
    # tolerate it (never raise on odd mail) and fall back to a lossy decode (finding 2).
    raw = (
        b"From: a@example.com\r\n"
        b"To: support@acme.io\r\n"
        b"Subject: bad charset\r\n"
        b'Content-Type: text/plain; charset="not-a-real-charset"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
        b"hello body\r\n"
    )
    parsed = mime.parse(raw)
    assert isinstance(parsed, mime.ParsedEmail)
    assert parsed.from_addr == "a@example.com"
    assert "hello body" in parsed.text  # best-effort fallback decode


def test_reply_subject() -> None:
    assert mime.reply_subject("Need help") == "Re: Need help"
    assert mime.reply_subject("Re: Need help") == "Re: Need help"
    assert mime.reply_subject(None) == "Re:"


def test_build_outbound_threads_and_roundtrips() -> None:
    msg = mime.build_outbound(
        sender="support@acme.test",
        sender_name="Relay",
        to_addr="alice@example.com",
        reply_to="reply+tok@inbound.relay.dev",
        subject="Re: Need help",
        text_body="Thanks for reaching out.",
        message_id="<out1@acme.test>",
        in_reply_to="<abc@example.com>",
        references=["<abc@example.com>"],
    )
    assert msg["In-Reply-To"] == "<abc@example.com>"
    assert "<abc@example.com>" in msg["References"]
    parsed = mime.parse(mime.render_bytes(msg))
    assert parsed.in_reply_to == "<abc@example.com>"
    assert "Thanks for reaching out." in parsed.text
    assert parsed.from_addr == "support@acme.test"
