"""LLM provider abstraction (RFC-003 §9 / RFC-001 §9): two providers behind one interface.

Neko treats LLM providers as slow, flaky, expensive, rate-limited dependencies. The whole
subsystem binds against one small interface — :class:`LLMProvider` — with three capabilities the
turn pipeline needs: **streaming**, **tool calls**, and **token accounting**. Two implementations:

- :class:`DeterministicProvider` — the hermetic dev/test/CI default. No network, and **stable across
  processes** (blake2b-free: pure structural rules), so a turn replays byte-for-byte from
  ``agent_runs`` (RFC-003 §8) and the eval/red-team suites reproduce. It is a real, if simple,
  stand-in: it reads the task marker + delimited DATA blocks (see :mod:`protocol`) and produces
  grounded, cited answers *only from its given evidence* — so it structurally cannot follow an
  instruction smuggled inside retrieved/customer content (RFC-003 §6). Prod swaps in the HTTP model
  with zero pipeline change.
- :class:`HttpLLMProvider` — prod. A streaming, tool-calling, token-accounted OpenAI-compatible chat
  endpoint with per-call timeouts and bounded jittered retries (RFC-001 §9). Not exercised in CI (no
  key); wired so a frontier model is a settings flip. Circuit breaking / rate-limit pools / failover
  live one layer up in :mod:`resilience`.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from relay.modules.ai import protocol

# --- Domain types -------------------------------------------------------------


@dataclass(frozen=True)
class ChatMessage:
    """One prompt message. ``content`` is already framed by the prompt builder — untrusted spans
    are delimited DATA blocks (never raw), the system message carries the policy + task marker."""

    role: str  # system | user | assistant | tool
    content: str


@dataclass(frozen=True)
class ToolSpec:
    """An allowlisted tool the model may call (RFC-003 §5 actions). Schema-validated by the caller;
    a tool response is untrusted input, re-framed as a DATA block before it re-enters context."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: TokenUsage
    model: str
    finish_reason: str = "stop"
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class StreamChunk:
    """A streamed delta. The terminal chunk has ``done=True`` and carries the final ``usage``."""

    delta: str = ""
    done: bool = False
    usage: TokenUsage | None = None


# --- Errors (RFC-001 §9 — mapped to breaker/failover decisions in resilience) -----------------


class LLMError(Exception):
    """Base for provider failures — all trip the circuit breaker + trigger failover."""


class LLMTimeout(LLMError):
    """The call exceeded its timeout budget (the blackhole case — RFC-003 acceptance)."""


class LLMRateLimited(LLMError):
    """Provider 429 — back off + fail over to the secondary pool."""


class LLMProviderError(LLMError):
    """5xx / transport / malformed response."""


# --- Token accounting ---------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """A stable ~4-chars/token estimate (RFC-003 §9 accounting). Exact provider counts override this
    when the real API returns usage; the estimate keeps the ledger populated + reproducible."""
    return max(1, len(text) // 4)


def _messages_tokens(messages: list[ChatMessage]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


# --- Interface ----------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """The one interface the turn pipeline binds against (RFC-003 §9)."""

    name: str

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> LLMResponse: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> AsyncIterator[StreamChunk]: ...


# --- Deterministic (hermetic) provider ----------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Cheap, curated lexicons for the hermetic preflight simulation. A real cheap model does this in
# natural language; these keep CI deterministic + are exercised by the red-team suite.
_HANDOFF_PATTERNS = (
    "talk to a person",
    "talk to a human",
    "talk to someone",
    "speak to a person",
    "speak to a human",
    "speak to someone",
    "real person",
    "real human",
    "human agent",
    "live agent",
    "speak to an agent",
    "talk to an agent",
    "representative",
    "customer service rep",
)
_ABUSE_TERMS = frozenset(
    {"idiot", "stupid", "hate", "useless", "damn", "shit", "fuck", "moron", "trash"}
)
_SELF_HARM_TERMS = frozenset({"suicide", "kill myself", "self-harm", "harm myself", "end my life"})
# Non-ASCII-heavy text ⇒ treat as a non-English locale (the real model detects language properly).
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
# Policy tripwires: advice a model must not invent beyond sources (RFC-003 §6 output filters).
_POLICY_TERMS = frozenset({"lawsuit", "diagnosis", "prescription", "medication", "legal advice"})
# Filler dropped when deriving a search query / scoring groundedness (distinctive terms dominate).
_REWRITE_FILLER = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "to",
        "i",
        "my",
        "me",
        "please",
        "hi",
        "hello",
        "hey",
        "can",
        "could",
        "would",
        "you",
        "help",
        "want",
        "need",
        "do",
        "does",
        "is",
        "am",
        "how",
        "of",
        "for",
        "with",
        "on",
        "in",
        "it",
        "this",
        "that",
        "get",
        "there",
    }
)


def _first_sentences(text: str, n: int = 2) -> str:
    parts = [s.strip() for s in _SENTENCE_RE.split(text.strip()) if s.strip()]
    return " ".join(parts[:n]) if parts else text.strip()


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def distinctive_terms(text: str) -> set[str]:
    """Content words with filler dropped — the shared lexical signal for the grounding gate
    (:mod:`pipeline`) and the groundedness verifier below."""
    return _words(text) - _REWRITE_FILLER


class DeterministicProvider:
    """Hermetic, reproducible LLM stand-in (see module docstring). CI/dev default."""

    def __init__(self, name: str = "deterministic") -> None:
        self.name = name

    # -- public interface --
    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> LLMResponse:
        text = self._respond(messages, max_tokens=max_tokens)
        usage = TokenUsage(_messages_tokens(messages), estimate_tokens(text))
        return LLMResponse(text=text, usage=usage, model=model)

    async def stream(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> AsyncIterator[StreamChunk]:
        text = self._respond(messages, max_tokens=max_tokens)
        # Stream word-by-word so the first token is emitted immediately (first-token latency budget,
        # RFC-003 §5). Whitespace is preserved by streaming tokens with their trailing space.
        tokens = text.split(" ")
        for i, tok in enumerate(tokens):
            delta = tok if i == len(tokens) - 1 else tok + " "
            if delta:
                yield StreamChunk(delta=delta)
        usage = TokenUsage(_messages_tokens(messages), estimate_tokens(text))
        yield StreamChunk(done=True, usage=usage)

    # -- simulation --
    def _respond(self, messages: list[ChatMessage], *, max_tokens: int) -> str:
        system = " ".join(m.content for m in messages if m.role == "system")
        user = "\n".join(m.content for m in messages if m.role == "user")
        blocks = protocol.iter_data_blocks(user)
        by_label = {label: (meta, content) for label, meta, content in blocks}
        task = protocol.parse_task(system)

        if task == protocol.TASK_PREFLIGHT:
            return self._preflight(by_label, user)
        if task == protocol.TASK_REWRITE:
            return self._rewrite(by_label, user)
        if task == protocol.TASK_VERIFY:
            return self._verify(by_label)
        # default + TASK_GENERATE
        return self._generate(by_label, max_tokens=max_tokens)

    def _customer_text(self, by_label: dict[str, tuple[dict[str, str], str]], user: str) -> str:
        if "msg" in by_label:
            return by_label["msg"][1]
        # No DATA block (shouldn't happen from the builder) → fall back to the whole user turn.
        return user

    def _preflight(self, by_label: dict[str, tuple[dict[str, str], str]], user: str) -> str:
        text = self._customer_text(by_label, user)
        low = text.lower()
        safety = "ok"
        if any(t in low for t in _SELF_HARM_TERMS):
            safety = "self_harm"
        elif any(t in low for t in _ABUSE_TERMS):
            safety = "abuse"
        handoff = any(p in low for p in _HANDOFF_PATTERNS)
        language = "und" if len(_NON_ASCII_RE.findall(text)) > max(3, len(text) // 5) else "en"
        is_question = "?" in text or bool(
            _words(text) & {"how", "what", "why", "when", "where", "who", "can", "do", "is"}
        )
        return json.dumps(
            {
                protocol.PREFLIGHT_LANGUAGE: language,
                protocol.PREFLIGHT_SAFETY: safety,
                protocol.PREFLIGHT_HANDOFF: handoff,
                protocol.PREFLIGHT_IS_QUESTION: is_question,
            }
        )

    def _rewrite(self, by_label: dict[str, tuple[dict[str, str], str]], user: str) -> str:
        text = self._customer_text(by_label, user)
        # A cheap, reproducible "search query" from the message: distinctive tokens, filler dropped.
        tokens = [t for t in _WORD_RE.findall(text.lower()) if t not in _REWRITE_FILLER]
        query = " ".join(tokens) or text.strip()
        return json.dumps({protocol.REWRITE_QUERY: query})

    def _generate(self, by_label: dict[str, tuple[dict[str, str], str]], *, max_tokens: int) -> str:
        chunks = [
            (lbl, meta, content)
            for lbl, (meta, content) in by_label.items()
            if _is_evidence_label(lbl)
        ]
        if not chunks:
            # No evidence reached generation — never fabricate (RFC-003 §6). Signal handoff.
            return "I don't have enough information to answer that."
        question = self._customer_text(by_label, "")
        q_words = _words(question)
        # Ground on the evidence most relevant to the question; c1 (top rank) breaks ties.
        ranked = sorted(
            chunks,
            key=lambda c: (len(_words(c[2]) & q_words), -_label_rank(c[0])),
            reverse=True,
        )
        top = ranked[0]
        body = _first_sentences(top[2], n=2)
        budget_words = max(20, max_tokens)  # words ~ tokens for the hermetic stand-in
        answer = " ".join(body.split(" ")[:budget_words])
        # Cite the grounding chunk — generation MUST carry a citation (RFC-003 §6).
        return f"{answer} {protocol.cite(top[0])}"

    def _verify(self, by_label: dict[str, tuple[dict[str, str], str]]) -> str:
        answer = by_label.get("answer", ({}, ""))[1]
        evidence = " ".join(
            content for lbl, (meta, content) in by_label.items() if _is_evidence_label(lbl)
        )
        ev_words = _words(evidence)
        unsupported: list[str] = []
        sentence_scores: list[float] = []
        for sentence in (s.strip() for s in _SENTENCE_RE.split(answer) if s.strip()):
            s_words = _words(protocol.strip_citations(sentence)) - _REWRITE_FILLER
            if not s_words:
                continue
            support = len(s_words & ev_words) / len(s_words)
            sentence_scores.append(support)
            # A sentence whose content words are mostly absent from the evidence is ungrounded — the
            # planted-claim rejection (RFC-003 acceptance). 0.5 = at least half its terms grounded.
            if support < 0.5:
                unsupported.append(sentence)
        low_answer = answer.lower()
        policy_flags = [t for t in _POLICY_TERMS if t in low_answer and t not in ev_words]
        score = min(sentence_scores) if sentence_scores else 0.0
        grounded = not unsupported and not policy_flags
        return json.dumps(
            {
                protocol.VERIFY_GROUNDED: grounded,
                protocol.VERIFY_SCORE: round(score, 4),
                protocol.VERIFY_UNSUPPORTED: unsupported,
                protocol.VERIFY_POLICY_FLAGS: policy_flags,
            }
        )


def _is_evidence_label(label: str) -> bool:
    """Evidence chunks are labelled ``c1``, ``c2`` … — strictly, so the ``hist``/``msg``/``answer``
    blocks are never mistaken for evidence (a label collision once fed context into generation)."""
    return len(label) >= 2 and label[0] == "c" and label[1:].isdigit()


def _label_rank(label: str) -> int:
    """``c1`` → 1, ``c2`` → 2 … (retrieval rank; lower is better)."""
    m = re.search(r"\d+", label)
    return int(m.group()) if m else 999


# --- HTTP (production) provider -----------------------------------------------


class HttpLLMProvider:
    """Prod: a streaming, tool-calling, token-accounted OpenAI-compatible chat endpoint.

    Every call is timed out; transient transport/5xx are retried a bounded, jittered number of
    times (RFC-001 §9). A 429 raises :class:`LLMRateLimited` and a timeout :class:`LLMTimeout` so
    :mod:`resilience` can trip the breaker + fail over. Not exercised in CI (no ``api_key``).
    """

    def __init__(
        self, *, name: str, api_base: str, api_key: str, organization: str | None = None
    ) -> None:
        self.name = name
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._org = organization

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if self._org:
            h["OpenAI-Organization"] = self._org
        return h

    def _body(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        return body

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> LLMResponse:
        body = self._body(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        data = await self._post_json(body, timeout_s=timeout_s)
        choice = data["choices"][0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"].get("arguments") or "{}"),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        return LLMResponse(
            text=msg.get("content") or "",
            usage=TokenUsage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=tool_calls,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_s: float = 20.0,
    ) -> AsyncIterator[StreamChunk]:
        import httpx

        body = self._body(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        prompt_tokens = _messages_tokens(messages)
        completion_tokens = 0
        try:
            async with (
                httpx.AsyncClient(timeout=timeout_s) as client,
                client.stream(
                    "POST",
                    f"{self._api_base}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as resp,
            ):
                _raise_for_status(resp)
                usage: TokenUsage | None = None
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    if event.get("usage"):
                        u = event["usage"]
                        usage = TokenUsage(
                            u.get("prompt_tokens", prompt_tokens),
                            u.get("completion_tokens", completion_tokens),
                        )
                    for choice in event.get("choices", []):
                        delta = (choice.get("delta") or {}).get("content") or ""
                        if delta:
                            completion_tokens += estimate_tokens(delta)
                            yield StreamChunk(delta=delta)
            yield StreamChunk(
                done=True, usage=usage or TokenUsage(prompt_tokens, completion_tokens)
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(str(exc)) from exc

    async def _post_json(self, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        import httpx
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_random_exponential,
        )

        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_random_exponential(multiplier=0.4, max=6),
            retry=retry_if_exception_type(LLMProviderError),
        )
        async def _once() -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(
                        f"{self._api_base}/chat/completions", headers=self._headers(), json=body
                    )
                    _raise_for_status(resp)
                    return resp.json()  # type: ignore[no-any-return]
            except httpx.TimeoutException as exc:
                raise LLMTimeout(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise LLMProviderError(str(exc)) from exc

        return await _once()


def _raise_for_status(resp: Any) -> None:
    """Map HTTP status to the typed LLM errors the breaker/failover understand."""
    if resp.status_code == 429:
        raise LLMRateLimited("provider rate limited (429)")
    if resp.status_code >= 500:
        raise LLMProviderError(f"provider {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMProviderError(f"provider client error {resp.status_code}")
