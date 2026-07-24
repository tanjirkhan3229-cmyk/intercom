"""Outbound email transport seam + provider circuit breaker (RFC-001 §6.6, §9; master rule 5).

The send task talks to an ``EmailSender``, never to a provider directly, so the transport is
swappable and testable: ``SmtpSender`` (dev → Mailpit), ``SesSender`` (staging/prod), ``FakeSender``
(tests, captures in-process). Every provider call is wrapped in an in-process ``CircuitBreaker`` so
a bounce storm / SES throttle trips the circuit instead of hammering a failing provider (RFC-001 §9
"circuit breakers on providers").
"""

from __future__ import annotations

import smtplib
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import boto3

from relay.core.logging import get_logger
from relay.settings import get_settings

log = get_logger(__name__)


class SendError(Exception):
    """A transient send failure — the send task retries it (bounded + jittered)."""


class EmailSender(Protocol):
    def send(self, *, raw: bytes, sender: str, recipients: list[str]) -> str:
        """Send raw MIME to ``recipients``; return the provider message id. Raise SendError on
        a transient failure."""
        ...


@dataclass
class SentEmail:
    sender: str
    recipients: list[str]
    raw: bytes


class FakeSender:
    """In-process test seam. Captures sends; ``fail_next`` forces N transient failures first."""

    def __init__(self) -> None:
        self.sent: list[SentEmail] = []
        self.fail_next: int = 0

    def send(self, *, raw: bytes, sender: str, recipients: list[str]) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise SendError("forced test failure")
        self.sent.append(SentEmail(sender=sender, recipients=list(recipients), raw=raw))
        # Globally unique id (like a real SES MessageId) so provider-id lookups never collide
        # across workspaces — the outbound engagement resolver relies on that uniqueness.
        return f"fake-{uuid.uuid4().hex}"

    def reset(self) -> None:
        self.sent.clear()
        self.fail_next = 0


class SmtpSender:
    """Dev transport → the Mailpit SMTP sink (settings.smtp_host/port)."""

    def send(self, *, raw: bytes, sender: str, recipients: list[str]) -> str:
        s = get_settings()
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
                smtp.sendmail(sender, recipients, raw)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(str(exc)) from exc
        return f"smtp-{uuid.uuid4().hex}"


class SesSender:
    """Staging/prod transport → SES ``send_raw_email`` (verified domain enforced by SES)."""

    def send(self, *, raw: bytes, sender: str, recipients: list[str]) -> str:
        s = get_settings()
        client = boto3.client(
            "ses",
            region_name=s.ses_region,
            endpoint_url=s.ses_endpoint_url,
            aws_access_key_id=s.ses_access_key_id,
            aws_secret_access_key=s.ses_secret_access_key,
        )
        try:
            resp = client.send_raw_email(
                Source=sender, Destinations=recipients, RawMessage={"Data": raw}
            )
        except Exception as exc:  # botocore ClientError / endpoint errors → transient
            raise SendError(str(exc)) from exc
        return str(resp["MessageId"])


@dataclass
class CircuitBreaker:
    """Wrap an ``EmailSender``; open the circuit after ``threshold`` consecutive failures and
    reject fast for ``cooldown`` seconds (half-open on the next attempt after cooldown)."""

    sender: EmailSender
    threshold: int = 5
    cooldown: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def send(self, *, raw: bytes, sender: str, recipients: list[str]) -> str:
        if self._opened_at is not None:
            if time.monotonic() - self._opened_at < self.cooldown:
                raise SendError("email circuit breaker open")
            self._opened_at = None  # cooldown elapsed → allow one half-open probe
        try:
            mid = self.sender.send(raw=raw, sender=sender, recipients=recipients)
        except SendError:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()
                log.error("channels.email.breaker_open", failures=self._failures)
            raise
        self._failures = 0
        return mid


# Process-wide FakeSender so a worker task and a test observe the same captured sends.
_FAKE = FakeSender()
_BREAKER: CircuitBreaker | None = None


def fake_sender() -> FakeSender:
    """The shared FakeSender (tests inspect ``.sent`` / set ``.fail_next``)."""
    return _FAKE


def _base_sender() -> EmailSender:
    transport = get_settings().email_transport
    if transport == "ses":
        return SesSender()
    if transport == "smtp":
        return SmtpSender()
    return _FAKE  # "memory"


def get_sender() -> CircuitBreaker:
    """Return the breaker-wrapped transport selected by ``settings.email_transport``."""
    global _BREAKER
    if _BREAKER is None:
        _BREAKER = CircuitBreaker(_base_sender())
    return _BREAKER


def reset_sender() -> None:
    """Test hook: rebuild the sender (after changing ``email_transport``) and clear the fake."""
    global _BREAKER
    _BREAKER = None
    _FAKE.reset()
