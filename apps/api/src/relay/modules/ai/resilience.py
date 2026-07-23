"""Provider resilience: circuit breakers, rate-limit pools, timeout budgets, failover, tiering.

This is the layer that turns two flaky providers (:mod:`providers`) into one dependable model
service (RFC-001 §9, RFC-003 §9). It owns:

- **Model tiering.** ``cheap`` tier (preflight/rewrite/verify) vs ``frontier`` tier (generation).
  Each route declares its model id + price per tier; the pipeline asks for a *tier*, never a
  model, so re-tiering is config.
- **Per-provider circuit breakers.** In-process, one per route: open after N consecutive failures,
  fast-fail for a cooldown, half-open probe after. A blackholed provider stops being tried within
  one turn, so the secondary carries the load (RFC-003 acceptance: fail over mid-conversation).
- **Rate-limit pools.** One bounded async semaphore per provider (concurrency cap). Per-*workspace*
  capping is the ai.interactive Celery concurrency cap (RFC-001 §6.4) — noted, not re-done here.
- **Timeout budgets + failover order.** Every call is timed out (the blackhole surfaces as
  :class:`LLMTimeout`); the router walks providers in order, skipping open breakers, until one
  answers or all are exhausted (:class:`AllProvidersFailed` ⇒ pipeline degrades to a human handoff,
  never silence). Streaming fails over on the *first token*: a provider that never produces one is
  abandoned before any delta reaches the customer, so failover is invisible.
- **Global model-route kill switch.** ``route_override`` pins/auto/── (RFC-003 §6 kill switches).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from relay.core.logging import get_logger
from relay.modules.ai.providers import (
    ChatMessage,
    DeterministicProvider,
    HttpLLMProvider,
    LLMError,
    LLMProvider,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)
from relay.settings import Settings, get_settings

log = get_logger(__name__)

Tier = Literal["cheap", "frontier"]


class AllProvidersFailed(LLMError):
    """Every provider in the failover order failed or was breaker-open (degrade to human)."""


# --- Pricing (RFC-003 §9; USD per 1M tokens, re-priced quarterly) -------------


@dataclass(frozen=True)
class TierPricing:
    input_per_m: float
    output_per_m: float

    def cost(self, usage: TokenUsage) -> float:
        return (
            usage.input_tokens / 1_000_000 * self.input_per_m
            + usage.output_tokens / 1_000_000 * self.output_per_m
        )


# Street-ish mid-2026 defaults; the deterministic route uses them too so the hermetic ledger carries
# realistic (and reproducible) cost numbers for analytics/eval budgets.
_DEFAULT_PRICING: dict[Tier, TierPricing] = {
    "cheap": TierPricing(0.15, 0.60),
    "frontier": TierPricing(2.50, 10.0),
}


# --- In-process circuit breaker (one per provider route) ----------------------


@dataclass
class InProcessBreaker:
    """Open after ``threshold`` consecutive failures; fast-fail for ``cooldown`` s; half-open probe
    after. One instance per provider per process (the router is a process singleton)."""

    threshold: int = 4
    cooldown: float = 15.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown:
            self._opened_at = None  # cooldown elapsed → allow one half-open probe
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()


# --- Provider route + failover router -----------------------------------------


@dataclass
class ProviderRoute:
    """A provider plus its per-tier models/pricing and its breaker + concurrency pool."""

    provider: LLMProvider
    cheap_model: str
    frontier_model: str
    breaker: InProcessBreaker = field(default_factory=InProcessBreaker)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))
    pricing: dict[Tier, TierPricing] = field(default_factory=lambda: dict(_DEFAULT_PRICING))

    @property
    def name(self) -> str:
        return self.provider.name

    def model_for(self, tier: Tier) -> str:
        return self.cheap_model if tier == "cheap" else self.frontier_model


@dataclass
class RouterResult:
    response: LLMResponse
    provider: str
    model: str
    cost_usd: float


@dataclass
class StreamOutcome:
    """Filled by :meth:`LLMRouter.stream` as its generator is consumed (async generators can't
    return a value). The pipeline reads it after the stream drains."""

    provider: str | None = None
    model: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    first_token_ms: float | None = None


class LLMRouter:
    """Failover across an ordered list of provider routes with breakers + tiering (RFC-001 §9)."""

    def __init__(self, routes: list[ProviderRoute], *, route_override: str = "auto") -> None:
        if not routes:
            raise ValueError("LLMRouter needs at least one provider route")
        self._routes = routes
        self._override = route_override

    def _ordered(self) -> list[ProviderRoute]:
        """Failover order honouring the global model-route flag (RFC-003 §6). ``auto`` = declared
        order; a provider name pins that route first (still falling back to the rest)."""
        if self._override in ("auto", "", "off"):
            return self._routes
        pinned = [r for r in self._routes if r.name == self._override]
        rest = [r for r in self._routes if r.name != self._override]
        return pinned + rest if pinned else self._routes

    async def complete(
        self,
        *,
        tier: Tier,
        messages: list[ChatMessage],
        max_tokens: int = 512,
        timeout_s: float = 20.0,
        tools: list[ToolSpec] | None = None,
    ) -> RouterResult:
        errors: list[str] = []
        for route in self._ordered():
            if route.breaker.is_open():
                errors.append(f"{route.name}: breaker open")
                continue
            model = route.model_for(tier)
            try:
                async with route.semaphore:
                    resp = await asyncio.wait_for(
                        route.provider.complete(
                            model=model,
                            messages=messages,
                            tools=tools,
                            max_tokens=max_tokens,
                            temperature=0.0,
                            timeout_s=timeout_s,
                        ),
                        timeout=timeout_s,
                    )
            except (LLMError, TimeoutError) as exc:
                route.breaker.record_failure()
                errors.append(f"{route.name}: {type(exc).__name__}")
                log.warning("ai.provider.failover", provider=route.name, tier=tier, error=str(exc))
                continue
            route.breaker.record_success()
            cost = route.pricing[tier].cost(resp.usage)
            return RouterResult(response=resp, provider=route.name, model=model, cost_usd=cost)
        raise AllProvidersFailed("; ".join(errors))

    async def stream(
        self,
        *,
        tier: Tier,
        messages: list[ChatMessage],
        outcome: StreamOutcome,
        max_tokens: int = 512,
        timeout_s: float = 20.0,
        first_token_timeout: float | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the first provider that yields a token within ``first_token_timeout``.

        A blackholed provider never produces a first token, so it is abandoned *before any delta
        reaches the customer* — the failover is user-invisible (RFC-003 acceptance). Once a provider
        commits (first token seen), the rest of its stream is trusted for this turn.
        """
        ft_budget = first_token_timeout if first_token_timeout is not None else timeout_s
        errors: list[str] = []
        for route in self._ordered():
            if route.breaker.is_open():
                errors.append(f"{route.name}: breaker open")
                continue
            model = route.model_for(tier)
            started = time.monotonic()
            async with route.semaphore:
                agen = route.provider.stream(
                    model=model,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    timeout_s=timeout_s,
                )
                try:
                    first = await asyncio.wait_for(_anext(agen), timeout=ft_budget)
                except (LLMError, TimeoutError, StopAsyncIteration) as exc:
                    route.breaker.record_failure()
                    errors.append(f"{route.name}: {type(exc).__name__}")
                    log.warning("ai.provider.stream_failover", provider=route.name, error=str(exc))
                    await _aclose(agen)
                    continue

                # Committed to this provider. Record which one served + first-token latency.
                route.breaker.record_success()
                outcome.provider = route.name
                outcome.model = model
                outcome.first_token_ms = (time.monotonic() - started) * 1000.0
                final_usage: TokenUsage | None = first.usage if first.done else None
                yield first
                async for chunk in agen:
                    if chunk.done:
                        final_usage = chunk.usage
                    yield chunk
                usage = final_usage or TokenUsage()
                outcome.usage = usage
                outcome.cost_usd = route.pricing[tier].cost(usage)
                return
        raise AllProvidersFailed("; ".join(errors))


async def _anext(agen: AsyncIterator[StreamChunk]) -> StreamChunk:
    return await agen.__anext__()


async def _aclose(agen: AsyncIterator[StreamChunk]) -> None:
    aclose = getattr(agen, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive cleanup
            await aclose()


# --- Factory ------------------------------------------------------------------


def _build_provider(name: str, settings: Settings) -> LLMProvider:
    if name == "deterministic":
        return DeterministicProvider(name="deterministic")
    # HTTP providers ("primary"/"secondary") read their own api_base/api_key from settings.
    api_base = getattr(settings, f"ai_{name}_api_base", None)
    api_key = getattr(settings, f"ai_{name}_api_key", None)
    if not api_base or not api_key:
        raise RuntimeError(f"ai provider {name!r} requires ai_{name}_api_base + ai_{name}_api_key")
    return HttpLLMProvider(name=name, api_base=api_base, api_key=api_key)


def build_router(settings: Settings | None = None) -> LLMRouter:
    """Construct the configured failover router. Defaults to one deterministic route (CI/dev)."""
    settings = settings or get_settings()
    routes: list[ProviderRoute] = []
    for name in settings.ai_provider_order:
        routes.append(
            ProviderRoute(
                provider=_build_provider(name, settings),
                cheap_model=settings.ai_cheap_model,
                frontier_model=settings.ai_frontier_model,
                semaphore=asyncio.Semaphore(settings.ai_provider_concurrency),
                breaker=InProcessBreaker(
                    threshold=settings.ai_breaker_threshold,
                    cooldown=settings.ai_breaker_cooldown_seconds,
                ),
            )
        )
    return LLMRouter(routes, route_override=settings.ai_model_route)


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """The process-cached router — so per-provider breaker state persists across turns in a worker
    (a blackholed provider stays skipped for its cooldown, not re-probed every turn)."""
    global _router
    if _router is None:
        _router = build_router()
    return _router


def reset_router() -> None:
    """Test hook: drop the cached router (e.g. after changing provider settings)."""
    global _router
    _router = None
