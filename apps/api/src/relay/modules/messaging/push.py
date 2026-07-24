"""APNs + FCM push adapters for the mobile SDKs (P1.10, RFC-000 §2.1).

Mirrors ``channels.sender``: a ``Pusher`` Protocol with an in-process ``FakePusher`` test seam,
concrete APNs/FCM senders, and a per-provider ``CircuitBreaker`` (master rule 5). Timeouts live
inside each concrete sender; **retries are the Celery task's job** (bounded + jittered — see
``messaging.tasks.send_push``). Sends are synchronous/blocking and run only on the worker's
per-process loop under ``run_coro`` (never on an API request path), exactly like the email sender.

Failures split two ways:

* ``PushTokenInvalid`` — the provider says the token is dead (APNs ``410`` / ``BadDeviceToken``,
  FCM ``UNREGISTERED``). **Terminal**: the fan-out marks the device ``stale`` and moves on. Never
  trips the breaker (a dead token isn't a provider-health signal).
* ``PushSendError``    — a transient provider/network failure. The task retries it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from relay.core.logging import get_logger
from relay.settings import get_settings

log = get_logger(__name__)


class PushSendError(Exception):
    """A transient push failure — the send task retries it (bounded + jittered)."""


class PushTokenInvalid(Exception):
    """The provider rejected the token as unregistered/invalid — mark the device ``stale``."""


@dataclass
class PushMessage:
    platform: str  # "ios" | "android"
    token: str
    title: str
    body: str
    topic: str | None  # APNs bundle id; None → settings.apns_default_topic
    environment: str  # "production" | "sandbox" (APNs host selection; ignored by FCM)
    data: dict[str, str]  # deep-link payload, e.g. {"conversation_id": "cnv_…"}


class Pusher(Protocol):
    def send(self, msg: PushMessage) -> str:
        """Deliver one notification; return the provider message id. Raise ``PushTokenInvalid``
        (dead token) or ``PushSendError`` (transient)."""
        ...


class FakePusher:
    """In-process test seam. Captures sends; ``fail_next`` forces N transient failures first;
    tokens added to ``invalid`` raise ``PushTokenInvalid`` (exercises stale-marking)."""

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []
        self.fail_next: int = 0
        self.invalid: set[str] = set()

    def send(self, msg: PushMessage) -> str:
        if msg.token in self.invalid:
            raise PushTokenInvalid(msg.token)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise PushSendError("forced test failure")
        self.sent.append(msg)
        return f"fake-{len(self.sent)}"

    def reset(self) -> None:
        self.sent.clear()
        self.fail_next = 0
        self.invalid.clear()


class ApnsPusher:
    """APNs over HTTP/2 with token-based (.p8 / ES256 JWT) auth. The provider JWT is valid <1h;
    we cache and refresh it well inside that window. Requires ``h2`` at send time (prod dep)."""

    _PROD_HOST = "https://api.push.apple.com"
    _SANDBOX_HOST = "https://api.sandbox.push.apple.com"
    _JWT_REFRESH_SECONDS = 3000  # <50 min — comfortably under APNs' 1 h ceiling

    def __init__(self) -> None:
        self._jwt: tuple[str, float] | None = None  # (token, monotonic issued-at)

    def _bearer(self) -> str:
        s = get_settings()
        now = time.monotonic()
        if self._jwt is not None and now - self._jwt[1] < self._JWT_REFRESH_SECONDS:
            return self._jwt[0]
        if not s.apns_private_key:
            raise PushSendError("APNs private key not configured")
        import jwt as pyjwt

        token = pyjwt.encode(
            {"iss": s.apns_team_id, "iat": int(time.time())},
            s.apns_private_key,
            algorithm="ES256",
            headers={"kid": s.apns_key_id},
        )
        self._jwt = (token, now)
        return token

    def send(self, msg: PushMessage) -> str:
        s = get_settings()
        topic = msg.topic or s.apns_default_topic
        if not topic:
            raise PushSendError("no APNs topic (register app_id or set apns_default_topic)")
        sandbox = s.apns_use_sandbox or msg.environment == "sandbox"
        host = self._SANDBOX_HOST if sandbox else self._PROD_HOST
        payload: dict[str, Any] = {
            "aps": {"alert": {"title": msg.title, "body": msg.body}, "sound": "default"}
        }
        payload.update(msg.data)

        import httpx

        try:
            with httpx.Client(http2=True, timeout=s.push_send_timeout_seconds) as client:
                resp = client.post(
                    f"{host}/3/device/{msg.token}",
                    json=payload,
                    headers={
                        "authorization": f"bearer {self._bearer()}",
                        "apns-topic": topic,
                        "apns-push-type": "alert",
                    },
                )
        except httpx.HTTPError as exc:  # timeout / connection → transient
            raise PushSendError(str(exc)) from exc
        if resp.status_code == 200:
            return str(resp.headers.get("apns-id", ""))
        # 410 Gone, or 400 with reason BadDeviceToken/DeviceTokenNotForTopic → token is dead.
        if resp.status_code == 410 or (
            resp.status_code == 400 and "BadDeviceToken" in resp.text
        ):
            raise PushTokenInvalid(msg.token)
        raise PushSendError(f"apns {resp.status_code}: {resp.text[:200]}")


class FcmPusher:
    """FCM HTTP v1. Mints an OAuth2 access token from the service-account JSON (RS256 assertion →
    Google token endpoint), caches it, then POSTs to the v1 ``send`` endpoint."""

    _SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
    _ACCESS_REFRESH_SECONDS = 3000

    def __init__(self) -> None:
        self._access: tuple[str, float] | None = None  # (token, monotonic issued-at)

    def _credentials(self) -> dict[str, Any]:
        import json

        return cast("dict[str, Any]", json.loads(get_settings().fcm_credentials_json or "{}"))

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._access is not None and now - self._access[1] < self._ACCESS_REFRESH_SECONDS:
            return self._access[0]
        import jwt as pyjwt

        creds = self._credentials()
        token_uri = creds.get("token_uri", "https://oauth2.googleapis.com/token")
        issued = int(time.time())
        assertion = pyjwt.encode(
            {
                "iss": creds["client_email"],
                "scope": self._SCOPE,
                "aud": token_uri,
                "iat": issued,
                "exp": issued + 3600,
            },
            creds["private_key"],
            algorithm="RS256",
        )
        import httpx

        try:
            resp = httpx.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=get_settings().push_send_timeout_seconds,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PushSendError(f"fcm token exchange: {exc}") from exc
        access = str(resp.json()["access_token"])
        self._access = (access, now)
        return access

    def send(self, msg: PushMessage) -> str:
        creds = self._credentials()
        project_id = get_settings().fcm_project_id or creds.get("project_id")
        if not project_id:
            raise PushSendError("no FCM project id")
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        message = {
            "token": msg.token,
            "notification": {"title": msg.title, "body": msg.body},
            "data": msg.data,
        }

        import httpx

        try:
            resp = httpx.post(
                url,
                json={"message": message},
                headers={"authorization": f"Bearer {self._access_token()}"},
                timeout=get_settings().push_send_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PushSendError(str(exc)) from exc
        if resp.status_code == 200:
            return str(resp.json().get("name", ""))
        # 404 / 400 UNREGISTERED|INVALID_ARGUMENT → the registration token is dead.
        if resp.status_code == 404 or (
            resp.status_code in (400, 403) and "UNREGISTERED" in resp.text
        ):
            raise PushTokenInvalid(msg.token)
        raise PushSendError(f"fcm {resp.status_code}: {resp.text[:200]}")


@dataclass
class CircuitBreaker:
    """In-process breaker (one per provider, one worker process). Opens after ``threshold``
    consecutive transient failures; a dead token (``PushTokenInvalid``) is not a fault."""

    pusher: Pusher
    threshold: int
    cooldown: float
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def send(self, msg: PushMessage) -> str:
        if self._opened_at is not None:
            if time.monotonic() - self._opened_at < self.cooldown:
                raise PushSendError("push circuit breaker open")
            self._opened_at = None  # cooldown elapsed → allow one half-open probe
        try:
            mid = self.pusher.send(msg)
        except PushTokenInvalid:
            raise  # token problem, not provider health → don't count it
        except PushSendError:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()
                log.error("messaging.push.breaker_open", failures=self._failures)
            raise
        self._failures = 0
        return mid


class PushDispatcher:
    """Routes a ``PushMessage`` to the right provider by platform."""

    def __init__(self, *, ios: Pusher, android: Pusher) -> None:
        self._by_platform: dict[str, Pusher] = {"ios": ios, "android": android}

    def send(self, msg: PushMessage) -> str:
        pusher = self._by_platform.get(msg.platform)
        if pusher is None:
            raise PushSendError(f"no pusher for platform {msg.platform!r}")
        return pusher.send(msg)


# Process-wide fake so a worker task and a test observe the same captured sends.
_FAKE = FakePusher()
_DISPATCHER: PushDispatcher | None = None


def fake_pusher() -> FakePusher:
    return _FAKE


def _wrap(p: Pusher) -> CircuitBreaker:
    s = get_settings()
    return CircuitBreaker(
        p, threshold=s.push_breaker_threshold, cooldown=s.push_breaker_cooldown_seconds
    )


def get_pusher() -> PushDispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        s = get_settings()
        if s.push_transport == "memory":
            _DISPATCHER = PushDispatcher(ios=_FAKE, android=_FAKE)
        else:
            # ponytail: an unconfigured live provider falls back to the fake (captures, warns) so a
            # half-configured deploy doesn't crash-loop; configure both providers in prod.
            ios: Pusher = _wrap(ApnsPusher()) if s.apns_configured else _FAKE
            android: Pusher = _wrap(FcmPusher()) if s.fcm_configured else _FAKE
            if not (s.apns_configured and s.fcm_configured):
                log.warning(
                    "messaging.push.provider_unconfigured",
                    apns=s.apns_configured,
                    fcm=s.fcm_configured,
                )
            _DISPATCHER = PushDispatcher(ios=ios, android=android)
    return _DISPATCHER


def reset_pusher() -> None:
    global _DISPATCHER
    _DISPATCHER = None
    _FAKE.reset()
