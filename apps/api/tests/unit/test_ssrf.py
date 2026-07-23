"""SSRF egress guard (P0.11, RFC-001 §10). DNS is monkeypatched so tests never hit the network."""

from __future__ import annotations

import socket

import pytest

from relay.core import ssrf
from relay.core.ssrf import SsrfError


def _fake_getaddrinfo(ip: str):  # type: ignore[no-untyped-def]
    def _f(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, port) if family == socket.AF_INET else (ip, port, 0, 0)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    return _f


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "172.16.0.1",
        "::1",
        "fd00:ec2::254",
    ],
)
def test_blocks_private_and_metadata(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(SsrfError):
        ssrf.validate_target("https://evil.example.com/hook", allow_private=False)


def test_allows_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert ssrf.validate_target("https://example.com/hook", allow_private=False) == "93.184.216.34"


def test_requires_https_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    with pytest.raises(SsrfError):
        ssrf.validate_target("http://example.com/hook", allow_private=False)


def test_allow_private_permits_localhost_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert ssrf.validate_target("http://127.0.0.1:9000/hook", allow_private=True) == "127.0.0.1"


def test_rejects_missing_host() -> None:
    with pytest.raises(SsrfError):
        ssrf.validate_target("https:///nohost", allow_private=False)
