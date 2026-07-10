"""Shared, cross-replica backends for the operational stores (Redis).

The in-process ``InMemoryIdempotencyStore`` and ``FixedWindowRateLimiter`` are
correct on one node and **fail silently on a second replica** behind a load
balancer: each replica keeps its own idempotency cache (a retry that lands on a
different replica re-runs the mutation) and its own per-subject counters (the
effective budget becomes N x the configured cap). These Redis-backed
implementations put both in one shared place, so the guarantee holds across the
fleet. Wire them in *before* any fan-out — that is the correctness gate the
scalability audit flagged.

Redis is an optional dependency (the ``redis`` extra); the import is deferred, so
a core install is unaffected. Each class takes an injected client (any object
with the handful of methods used — ``get``/``set`` or ``incr``/``expire``), so it
tests against a fake, and ``from_url`` builds one from a real ``redis.Redis`` in
production. They satisfy the same ``IdempotencyStore`` / ``RateLimiter`` protocols
as the in-memory defaults, so swapping is a one-line change at ``Hallpass(...)``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from .ratelimit import RateLimited

__all__ = ["RedisLike", "RedisIdempotencyStore", "RedisRateLimiter"]


class RedisLike(Protocol):
    """The subset of a Redis client these backends use. A real ``redis.Redis``
    satisfies it; so does a small fake in tests."""

    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: str, *, ex: int | None = None) -> Any: ...
    def incr(self, name: str) -> int: ...
    def expire(self, name: str, seconds: int) -> Any: ...


class RedisIdempotencyStore:
    """Idempotency cache shared across replicas via Redis. ``get`` reads and
    ``put`` writes with a TTL, so once any replica records a result every
    replica returns it on a repeat of the same ``(subject, tool, key)`` — the
    at-most-once guarantee that the in-memory store silently loses under a load
    balancer. Results are JSON-serialized (a networked cache holds bytes, not a
    live Python object), so a stored result must be JSON-serializable.

    Like the in-memory store this is get-then-put, so two *simultaneous*
    first-calls can still both miss; the common case — a retry after the first
    call recorded — is now strict across replicas. A stricter reserve-before-run
    variant is a protocol extension, noted in docs/IDEAS.md."""

    def __init__(
        self,
        client: RedisLike,
        *,
        ttl_seconds: float = 86_400.0,
        prefix: str = "hp:idem:",
    ) -> None:
        self._c = client
        self._ttl = int(ttl_seconds)
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisIdempotencyStore:
        """Build one from a Redis URL (requires the ``redis`` extra)."""
        import redis

        return cls(redis.Redis.from_url(url, decode_responses=True), **kwargs)

    def _key(self, subject: str, tool: str, key: str) -> str:
        # Hash the tuple so an arbitrary subject/tool/key can't collide or inject
        # a delimiter into the Redis key namespace.
        digest = hashlib.sha256(f"{subject}\x00{tool}\x00{key}".encode()).hexdigest()
        return f"{self._prefix}{digest}"

    def get(self, subject: str, tool: str, key: str) -> tuple[bool, Any]:
        raw = self._c.get(self._key(subject, tool, key))
        if raw is None:
            return False, None
        text = raw.decode() if isinstance(raw, bytes) else raw
        return True, json.loads(text)

    def put(self, subject: str, tool: str, key: str, result: Any) -> None:
        self._c.set(self._key(subject, tool, key), json.dumps(result), ex=self._ttl)


class RedisRateLimiter:
    """Per-subject rate limiting shared across replicas via Redis, so the
    per-subject budget is enforced *fleet-wide* rather than per-replica (the
    in-memory limiter's silent N-x-the-cap failure behind a load balancer).

    A fixed window via ``INCR`` + ``EXPIRE`` on a per-window key: cheap and
    atomic per counter (``INCR`` is atomic). It is a fixed window, not the
    sliding window of the in-memory limiter — the shared cap is the point; a
    burst can straddle a boundary, bounded by 2x within one window."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        client: RedisLike,
        *,
        prefix: str = "hp:rl:",
        now: Any = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        import time

        self._max = max_calls
        self._window = window_seconds
        self._c = client
        self._prefix = prefix
        self._now = now or time.time

    @classmethod
    def from_url(
        cls, max_calls: int, window_seconds: float, url: str, **kwargs: Any
    ) -> RedisRateLimiter:
        """Build one from a Redis URL (requires the ``redis`` extra)."""
        import redis

        return cls(max_calls, window_seconds, redis.Redis.from_url(url), **kwargs)

    def check(self, subject: str) -> None:
        bucket = int(self._now() // self._window)
        key = f"{self._prefix}{subject}:{bucket}"
        count = self._c.incr(key)
        if count == 1:
            # First hit in this window: set the key to expire when the window
            # ends (+1s slack), so counters do not accumulate forever.
            self._c.expire(key, int(self._window) + 1)
        if count > self._max:
            raise RateLimited(subject, self._max, self._window)
