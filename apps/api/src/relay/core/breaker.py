"""A tiny Redis-backed circuit breaker (P0.11, RFC-001 §6.7/§9).

Unlike the in-process ``channels/sender.py`` breaker (one Celery task family, one process), the
webhook breaker must be shared across *every* worker process delivering to the same endpoint, so
its state lives in Redis: a single key per subscription that opens after ``threshold`` consecutive
failures and rejects fast for ``cooldown`` seconds. Durable failure accounting (auto-disable) is
tracked separately on the subscription row; this is only the fast, cross-process "skip the HTTP
call" gate.
"""

from __future__ import annotations

from typing import Any


class RedisCircuitBreaker:
    """Cross-process breaker keyed by an arbitrary string (e.g. a subscription id).

    ``open`` is represented by the mere existence of the Redis key (TTL = ``cooldown``); it is set
    only once ``threshold`` consecutive failures have been recorded. A success clears both the
    failure counter and the open key (closing the breaker). Uses the synchronous Redis client
    because the only caller is the sync ``webhooks.deliver`` Celery task.
    """

    def __init__(
        self, redis: Any, key: str, *, threshold: int = 5, cooldown_seconds: int = 60
    ) -> None:
        self._redis = redis
        self._open_key = f"whk:breaker:open:{key}"
        self._fail_key = f"whk:breaker:fail:{key}"
        self._threshold = threshold
        self._cooldown = cooldown_seconds

    # Atomic incr-count-and-maybe-open, so a concurrent success/failure on the same subscription
    # can't interleave into an inconsistent (counter, open) state. KEYS = [open, fail];
    # ARGV = [threshold, fail_ttl, cooldown].
    _RECORD_FAILURE_LUA = """
    local failures = redis.call('INCR', KEYS[2])
    if failures == 1 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
    if failures >= tonumber(ARGV[1]) then
      redis.call('SET', KEYS[1], '1', 'EX', ARGV[3])
      return 1
    end
    return 0
    """

    def is_open(self) -> bool:
        """True if the breaker is open (fast-fail; skip the outbound call)."""
        return bool(self._redis.exists(self._open_key))

    def record_failure(self) -> bool:
        """Record one failure atomically. Returns True iff this failure just opened the breaker.

        The failure counter expires after a generous window (10x cooldown) so a slow trickle of
        unrelated failures doesn't accumulate forever; consecutive failures within the window trip.
        """
        opened = self._redis.eval(
            self._RECORD_FAILURE_LUA,
            2,
            self._open_key,
            self._fail_key,
            self._threshold,
            self._cooldown * 10,
            self._cooldown,
        )
        return bool(opened)

    def record_success(self) -> None:
        """Close the breaker and reset the failure counter."""
        self._redis.delete(self._open_key, self._fail_key)
