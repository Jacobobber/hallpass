"""At-most-once execution for retried tool calls.

Agents retry. A call times out, the network blips, a loop re-issues the same
step -- and if that step was a mutation (create an issue, send a message, charge
a card) the naive outcome is that it happens twice. An idempotency key lets the
caller say "this is the same operation as before": the first call runs and its
result is remembered under the key; a repeat returns that stored result instead
of running the handler again.

The store is injected. The in-memory default is correct for a single process
and best-effort under concurrency (two truly simultaneous first-calls with the
same key can both miss and run); a production deployment wires a store whose
put-if-absent is atomic (Redis SETNX, a unique DB constraint) via the same
protocol to make it strict. Keys are scoped to ``(subject, tool)`` so one
user's key can never return another user's result, and only successful results
are remembered -- a failed call leaves nothing, so a later retry can succeed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["IdempotencyStore", "InMemoryIdempotencyStore"]


class IdempotencyStore(Protocol):
    def get(self, subject: str, tool: str, key: str) -> tuple[bool, Any]:
        """Return ``(hit, result)``. ``hit`` is True when a result is stored
        for this ``(subject, tool, key)``; ``result`` is meaningful only then."""
        ...

    def put(self, subject: str, tool: str, key: str, result: Any) -> None:
        """Remember ``result`` for this ``(subject, tool, key)``."""
        ...


@dataclass
class _Entry:
    result: Any
    stored_at: float


class InMemoryIdempotencyStore:
    """Single-process idempotency cache with a TTL. Best-effort under
    concurrency; see the module docstring for the strict production path."""

    def __init__(self, *, ttl_seconds: float = 86_400.0, now: Any = time.time) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    def get(self, subject: str, tool: str, key: str) -> tuple[bool, Any]:
        entry = self._entries.get((subject, tool, key))
        if entry is None:
            return False, None
        if self._now() - entry.stored_at > self._ttl:
            del self._entries[(subject, tool, key)]
            return False, None
        return True, entry.result

    def put(self, subject: str, tool: str, key: str, result: Any) -> None:
        self._entries[(subject, tool, key)] = _Entry(result, self._now())
