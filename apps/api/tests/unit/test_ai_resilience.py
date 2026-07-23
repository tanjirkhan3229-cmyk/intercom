"""Unit tests for provider resilience: breakers, pools, failover, tiering (RFC-001 §9)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from relay.modules.ai.providers import (
    ChatMessage,
    DeterministicProvider,
    LLMResponse,
    LLMTimeout,
    StreamChunk,
    TokenUsage,
)
from relay.modules.ai.resilience import (
    AllProvidersFailed,
    InProcessBreaker,
    LLMRouter,
    ProviderRoute,
    StreamOutcome,
)


class Blackhole:
    """A provider that always times out (the RFC-003 blackhole)."""

    name = "blackhole"

    async def complete(self, **_kw: object) -> LLMResponse:
        raise LLMTimeout("blackhole")

    async def stream(self, **_kw: object) -> AsyncIterator[StreamChunk]:
        raise LLMTimeout("blackhole")
        yield StreamChunk()  # pragma: no cover — unreachable, makes this an async generator


class Counting:
    """Wraps the deterministic provider, counting how often it is called."""

    def __init__(self) -> None:
        self.name = "counting"
        self._inner = DeterministicProvider()
        self.completes = 0

    async def complete(self, **kw: object) -> LLMResponse:
        self.completes += 1
        return await self._inner.complete(**kw)  # type: ignore[arg-type]

    def stream(self, **kw: object) -> AsyncIterator[StreamChunk]:
        return self._inner.stream(**kw)  # type: ignore[arg-type]


def _route(provider: object, **kw: object) -> ProviderRoute:
    return ProviderRoute(
        provider=provider, cheap_model="cheap-1", frontier_model="frontier-1", **kw
    )  # type: ignore[arg-type]


def _preflight_msgs() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="[relay:task=preflight]"),
        ChatMessage(role="user", content="MESSAGE:\n⟦DATA msg⟧\nhi\n⟦/DATA⟧"),
    ]


def test_breaker_opens_then_half_opens() -> None:
    b = InProcessBreaker(threshold=2, cooldown=0.0)
    assert not b.is_open()
    b.record_failure()
    assert not b.is_open()
    b.record_failure()  # hits threshold
    # cooldown=0 ⇒ immediately eligible for a half-open probe
    assert not b.is_open()
    b.record_success()
    assert not b.is_open()


async def test_complete_fails_over_to_secondary() -> None:
    router = LLMRouter([_route(Blackhole()), _route(DeterministicProvider())])
    res = await router.complete(tier="cheap", messages=_preflight_msgs(), timeout_s=1.0)
    assert res.provider == "deterministic"
    assert res.model == "cheap-1"  # tiering: cheap tier ⇒ cheap model
    assert res.cost_usd > 0  # cost accounted from token usage


async def test_stream_fails_over_before_any_token() -> None:
    gen_msgs = [
        ChatMessage(role="system", content="[relay:task=generate]"),
        ChatMessage(
            role="user",
            content="EVIDENCE:\n⟦DATA c1 | kind=article⟧\nRefunds in 30 days.\n⟦/DATA⟧\n\n"
            "MESSAGE:\n⟦DATA msg⟧\nrefund?\n⟦/DATA⟧",
        ),
    ]
    router = LLMRouter([_route(Blackhole()), _route(DeterministicProvider())])
    outcome = StreamOutcome()
    deltas = [
        c.delta
        async for c in router.stream(
            tier="frontier",
            messages=gen_msgs,
            outcome=outcome,
            timeout_s=1.0,
            first_token_timeout=1.0,
        )
        if c.delta
    ]
    assert outcome.provider == "deterministic"  # the blackhole never produced a token
    assert outcome.first_token_ms is not None
    assert outcome.model == "frontier-1"  # frontier tier
    assert "".join(deltas)  # a real answer streamed from the secondary


async def test_all_providers_failed_raises() -> None:
    router = LLMRouter([_route(Blackhole())])
    with pytest.raises(AllProvidersFailed):
        await router.complete(tier="cheap", messages=_preflight_msgs(), timeout_s=0.5)


async def test_open_breaker_skips_provider() -> None:
    counting = Counting()
    breaker = InProcessBreaker(threshold=1, cooldown=60.0)
    breaker.record_failure()  # force-open the secondary's breaker
    router = LLMRouter([_route(Blackhole()), _route(counting, breaker=breaker)])
    with pytest.raises(AllProvidersFailed):
        await router.complete(tier="cheap", messages=_preflight_msgs(), timeout_s=0.5)
    assert counting.completes == 0  # open breaker ⇒ provider never called


async def test_route_override_pins_provider() -> None:
    det = DeterministicProvider()
    router = LLMRouter([_route(Blackhole()), _route(det)], route_override="deterministic")
    res = await router.complete(tier="cheap", messages=_preflight_msgs(), timeout_s=1.0)
    assert res.provider == "deterministic"  # pinned route tried first, blackhole skipped


def test_pricing_tiers_differ() -> None:
    from relay.modules.ai.resilience import _DEFAULT_PRICING

    usage = TokenUsage(input_tokens=1000, output_tokens=1000)
    assert _DEFAULT_PRICING["frontier"].cost(usage) > _DEFAULT_PRICING["cheap"].cost(usage)
