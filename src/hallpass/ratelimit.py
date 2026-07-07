"""Per-principal rate limiting for tool calls.

A multi-user bridge has to protect the tools behind it: one agent stuck
in a loop must not be able to hammer a downstream service on everyone's
behalf. The budget is per subject, so one caller's burst never starves
another's, and the limit is checked on the authenticated identity, not
on anything the caller asserts.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Protocol

__all__ = ["RateLimited", "RateLimiter", "FixedWindowRateLimiter"]


class RateLimited(Exception):
    """The principal exceeded its call budget for the current window."""

    def __init__(self, subject: str, limit: int, window_seconds: float) -> None:
        super().__init__(f"rate limit exceeded: {limit} calls per {window_seconds:g}s")
        self.subject = subject
        self.limit = limit
        self.window_seconds = window_seconds


class RateLimiter(Protocol):
    def check(self, subject: str) -> None:
        """Raise RateLimited if ``subject`` is over budget; return otherwise.
        Called once per accepted attempt, so a passing check also counts
        the attempt against the budget."""
        ...


class FixedWindowRateLimiter:
    """At most ``max_calls`` per ``window_seconds`` per subject, measured
    as a true sliding window (timestamps, not a coarse bucket that resets
    on a boundary). Thread-safe."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_calls
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, subject: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(subject, deque())
            cutoff = now - self._window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                raise RateLimited(subject, self._max, self._window)
            hits.append(now)
