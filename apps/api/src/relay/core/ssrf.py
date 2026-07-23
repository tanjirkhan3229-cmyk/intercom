"""SSRF egress guard for outbound HTTP to customer-controlled URLs (RFC-001 §10).

Webhook targets are attacker-influenced, so before connecting we: (1) require ``https`` (``http``
is allowed only when ``webhook_allow_private_targets`` is set — dev/tests); (2) resolve the host
and reject if *any* resolved address is private/loopback/link-local/reserved/multicast or a cloud
metadata IP; (3) **pin** the connection to a vetted address so a DNS rebind between our check and
the actual connect cannot smuggle us onto an internal IP. Redirects are refused by the caller (a
3xx to an internal URL would bypass every check).

Pinning is implemented by constraining ``socket.getaddrinfo`` for the target host to the vetted IP
for the duration of the request; TLS still uses the real hostname for SNI + certificate
verification (the URL is unchanged). A process-wide lock serialises pinned requests so the
override is race-free even under a worker thread pool (Celery prefork already runs one task per
process, and webhook volume per worker is low, so the lock is effectively free).
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from relay.core.errors import AppError

_PIN_LOCK = threading.Lock()
# Cloud instance-metadata endpoints (AWS/GCP IMDS, etc.) — classic SSRF targets.
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


class SsrfError(AppError):
    """A webhook target URL that is unsafe to call (bad scheme or resolves to a blocked IP)."""

    status_code = 422
    code = "invalid_webhook_url"


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → block
    if str(addr) in _METADATA_IPS:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"could not resolve webhook host {host!r}") from exc
    return [str(info[4][0]) for info in infos]


def validate_target(url: str, *, allow_private: bool) -> str:
    """Validate ``url`` against the egress policy and return the vetted IP to pin to.

    Raises :class:`SsrfError` on a bad scheme or a host resolving to any blocked address.
    ``allow_private`` (dev/tests) relaxes the https requirement and the IP checks so a localhost
    receiver works, but the host is still resolved (so a bad hostname still fails).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not host:
        raise SsrfError("webhook url has no host")
    if allow_private:
        if scheme not in ("http", "https"):
            raise SsrfError("webhook url must be http(s)")
    elif scheme != "https":
        raise SsrfError("webhook url must use https")
    port = parts.port or (443 if scheme == "https" else 80)
    ips = _resolve(host, port)
    if not ips:
        raise SsrfError(f"could not resolve webhook host {host!r}")
    if not allow_private:
        for ip in ips:
            if _ip_is_blocked(ip):
                raise SsrfError(f"webhook host {host!r} resolves to a blocked address")
    return ips[0]


def _accepted_hosts(host: str) -> frozenset[str]:
    """Host spellings a client may pass to getaddrinfo (lowercased / IDNA / no trailing dot).

    The pin must still match after the client normalises the host; mismatches fail closed, so this
    need only cover legitimate normalisations, not attacker input.
    """
    names = {host, host.lower(), host.rstrip(".")}
    with contextlib.suppress(UnicodeError, ValueError):
        names.add(host.encode("idna").decode("ascii"))  # IDN → punycode; no-op for ascii/IPs
    return frozenset(names)


@contextlib.contextmanager
def _pin_host(hosts: frozenset[str], ip: str, port: int) -> Iterator[None]:
    """Constrain ``socket.getaddrinfo`` so the target host resolves only to the vetted ``ip``.

    A lookup for any *other* host **fails closed** (raises) rather than falling through to a real
    DNS query — a fallthrough would reopen the very DNS-rebind window the pin exists to close (the
    client may re-resolve at connect time under a normalised/punycode host name)."""
    real = socket.getaddrinfo
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr: Any = (ip, port) if family == socket.AF_INET else (ip, port, 0, 0)

    def _patched(h: Any, p: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if h in hosts:
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
        raise OSError(f"ssrf: unexpected host resolution {h!r} during a pinned request")

    with _PIN_LOCK:
        socket.getaddrinfo = _patched  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = real


def guarded_post(
    url: str,
    *,
    content: bytes,
    headers: dict[str, str],
    timeout: float,
    allow_private: bool,
) -> httpx.Response:
    """Validate ``url``, pin DNS to the vetted IP, and POST (sync; called by ``webhooks.deliver``).

    Redirects are disabled so a 3xx cannot escape the guard. Raises :class:`SsrfError` if the URL
    is unsafe, or ``httpx`` transport errors on connect/timeout (the caller treats both as a
    retryable delivery failure)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    ip = validate_target(url, allow_private=allow_private)
    with _pin_host(_accepted_hosts(host), ip, port):
        return httpx.post(
            url, content=content, headers=headers, timeout=timeout, follow_redirects=False
        )
