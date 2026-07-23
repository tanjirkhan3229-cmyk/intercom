"""Unit tests for SNS signature verification + envelope parsing (P0.7, RFC-001 §6.6/§10)."""

from __future__ import annotations

import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relay.modules.channels import sns

_CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem"


def _signed_notification(private_key: rsa.RSAPrivateKey, message: str = "hello") -> dict:
    body = {
        "Type": "Notification",
        "MessageId": "m1",
        "Message": message,
        "Timestamp": "2026-07-23T00:00:00.000Z",
        "TopicArn": "arn:aws:sns:us-east-1:1:relay-inbound",
        "SignatureVersion": "1",
        "SigningCertURL": _CERT_URL,
    }
    to_sign = sns._string_to_sign(body)
    assert to_sign is not None
    signature = private_key.sign(to_sign.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    body["Signature"] = base64.b64encode(signature).decode("ascii")
    return body


async def test_valid_signature_verifies() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = _signed_notification(key)
    sns._CERT_CACHE[_CERT_URL] = key.public_key()  # skip network fetch
    async with httpx.AsyncClient() as client:
        assert await sns.verify(body, client=client) is True
    sns._CERT_CACHE.pop(_CERT_URL, None)


async def test_tampered_message_rejected() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = _signed_notification(key)
    body["Message"] = "tampered"  # signature no longer matches
    sns._CERT_CACHE[_CERT_URL] = key.public_key()
    async with httpx.AsyncClient() as client:
        assert await sns.verify(body, client=client) is False
    sns._CERT_CACHE.pop(_CERT_URL, None)


async def test_non_aws_cert_url_rejected() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = _signed_notification(key)
    body["SigningCertURL"] = "https://evil.example.com/cert.pem"
    async with httpx.AsyncClient() as client:
        assert await sns.verify(body, client=client) is False


def test_string_to_sign_orders_subscription_keys() -> None:
    body = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m",
        "Message": "confirm",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/confirm",
        "Timestamp": "t",
        "Token": "tok",
        "TopicArn": "arn",
    }
    result = sns._string_to_sign(body)
    assert result is not None
    # Canonical order: Message, MessageId, SubscribeURL, Timestamp, Token, TopicArn, Type.
    assert result.startswith("Message\nconfirm\nMessageId\nm\nSubscribeURL\n")
    assert result.endswith("Type\nSubscriptionConfirmation\n")


def test_subject_only_signed_when_present() -> None:
    without = sns._string_to_sign(
        {
            "Type": "Notification",
            "Message": "x",
            "MessageId": "m",
            "Timestamp": "t",
            "TopicArn": "a",
        }
    )
    assert without is not None
    assert "Subject" not in without


def test_parse_envelope() -> None:
    env = sns.parse_envelope({"Type": "Notification", "MessageId": "abc", "Message": "{}"})
    assert env.type == "Notification"
    assert env.message_id == "abc"


@pytest.mark.parametrize("bad", ["Notification", "SubscriptionConfirmation"])
def test_string_to_sign_missing_field_returns_none(bad: str) -> None:
    # Missing a required signable field → cannot build the canonical string.
    assert sns._string_to_sign({"Type": bad}) is None
