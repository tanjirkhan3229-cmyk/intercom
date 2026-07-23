"""Amazon SNS envelope handling for the inbound webhooks (RFC-001 §6.6, §10 platform security).

SES publishes inbound-receipt and bounce/complaint notifications to SNS, which POSTs a signed JSON
envelope to our webhook. We verify the signature (RSA over a canonical string-to-sign, cert fetched
from an ``*.amazonaws.com`` URL) before trusting anything, and auto-confirm subscription handshakes.

Signature verification is separable and gated by ``settings.sns_verify_signatures`` so tests can
inject a trusted fake while the verifier itself is covered by its own unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relay.core.logging import get_logger

log = get_logger(__name__)

# Fields signed by SNS, in canonical order, per message type (AWS SNS developer guide).
_SIGNABLE_KEYS: dict[str, tuple[str, ...]] = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
    "UnsubscribeConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
}

_CERT_CACHE: dict[str, rsa.RSAPublicKey] = {}

# Trust anchor: SNS signing certs + SubscribeURLs come ONLY from ``sns.<region>.amazonaws.com``.
# A broad ``*.amazonaws.com`` match would also accept attacker-controlled ``*.s3.amazonaws.com``
# (a forged cert hosted there would defeat webhook auth) — so pin the host exactly (review H1).
_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")


@dataclass
class SnsEnvelope:
    type: str
    message_id: str
    message: str
    topic_arn: str | None
    subject: str | None
    subscribe_url: str | None
    raw: dict[str, Any]


def parse_envelope(body: dict[str, Any]) -> SnsEnvelope:
    return SnsEnvelope(
        type=str(body.get("Type", "")),
        message_id=str(body.get("MessageId", "")),
        message=str(body.get("Message", "")),
        topic_arn=body.get("TopicArn"),
        subject=body.get("Subject"),
        subscribe_url=body.get("SubscribeURL"),
        raw=body,
    )


def _string_to_sign(body: dict[str, Any]) -> str | None:
    keys = _SIGNABLE_KEYS.get(str(body.get("Type", "")))
    if keys is None:
        return None
    parts: list[str] = []
    for key in keys:
        if key == "Subject" and key not in body:
            continue  # Subject is optional and only signed when present
        if key not in body:
            return None
        parts.append(f"{key}\n{body[key]}\n")
    return "".join(parts)


def _is_aws_cert_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(_SNS_HOST.match(parsed.hostname or ""))


async def _load_public_key(url: str, *, client: httpx.AsyncClient) -> rsa.RSAPublicKey | None:
    if url in _CERT_CACHE:
        return _CERT_CACHE[url]
    resp = await client.get(url)
    resp.raise_for_status()
    cert = x509.load_pem_x509_certificate(resp.content)
    key = cert.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        return None
    _CERT_CACHE[url] = key
    return key


async def verify(body: dict[str, Any], *, client: httpx.AsyncClient) -> bool:
    """Return True iff the SNS message signature is valid. Network-fetches the signing cert."""
    cert_url = body.get("SigningCertURL")
    if not _is_aws_cert_url(cert_url):
        log.warning("channels.sns.bad_cert_url", url=cert_url)
        return False
    signature_b64 = body.get("Signature")
    to_sign = _string_to_sign(body)
    if not signature_b64 or to_sign is None:
        return False

    import base64

    try:
        signature = base64.b64decode(signature_b64)
        public_key = await _load_public_key(str(cert_url), client=client)
        if public_key is None:
            return False
        algo = hashes.SHA256() if str(body.get("SignatureVersion")) == "2" else hashes.SHA1()
        public_key.verify(signature, to_sign.encode("utf-8"), padding.PKCS1v15(), algo)
        return True
    except (InvalidSignature, ValueError, httpx.HTTPError) as exc:
        log.warning("channels.sns.verify_failed", error=str(exc))
        return False


async def confirm_subscription(envelope: SnsEnvelope, *, client: httpx.AsyncClient) -> None:
    """Complete an SNS subscription handshake by visiting the SubscribeURL."""
    if envelope.subscribe_url and _is_aws_cert_url(envelope.subscribe_url):
        resp = await client.get(envelope.subscribe_url)
        resp.raise_for_status()
        log.info("channels.sns.subscription_confirmed", topic=envelope.topic_arn)
