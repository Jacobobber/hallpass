"""Shared, hot-path-safe token revocation for a fleet.

``InMemoryRevocationList`` (in ``identity``) is correct on one node but
per-process: a revoke on one replica does not reach the others. This module
makes revocation work across replicas without slowing the verify hot path:

- ``SqliteRevocationList`` / ``PostgresRevocationList`` (the latter in
  ``postgres_backends``) are durable, shared *sources* of truth.
- ``CachedRevocationList`` wraps a source with a short-TTL in-memory view, so
  ``is_revoked`` -- called on every verify -- is an O(1) set membership check,
  not a database read per token. A ``revoke``/``restore`` through the wrapper
  writes through to the source AND updates the local set at once, so the replica
  that acted sees it immediately and the rest converge within the TTL.

Revocation is deliberately eventually-consistent across replicas (bounded by the
TTL): the set is tiny and changes rarely, and an incident cutoff within a few
seconds fleet-wide is the honest guarantee -- far better than "at token exp".
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .identity import RevocationList

__all__ = ["RevocationStore", "SqliteRevocationList", "CachedRevocationList"]


class RevocationStore(RevocationList, Protocol):
    """A mutable revocation source: the read the verifier needs (``is_revoked``,
    from ``RevocationList``) plus the writes an operator/control plane makes."""

    def revoke(self, subject: str, *, reason: str = "") -> None: ...
    def restore(self, subject: str) -> None: ...
    def revoked(self) -> list[str]: ...


class SqliteRevocationList:
    """Durable revoked-subject set on SQLite. A single node, or a shared file for
    a small fleet; for many replicas use ``PostgresRevocationList``. Wrap in
    ``CachedRevocationList`` for the verify hot path."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS revocations ("
                " subject TEXT PRIMARY KEY, reason TEXT NOT NULL, at REAL NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def revoke(self, subject: str, *, reason: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO revocations (subject, reason, at) VALUES (?, ?, ?)"
                " ON CONFLICT(subject) DO UPDATE SET reason = excluded.reason",
                (subject, reason, time.time()),
            )

    def restore(self, subject: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM revocations WHERE subject = ?", (subject,))

    def is_revoked(self, subject: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM revocations WHERE subject = ?", (subject,)
            ).fetchone()
        return row is not None

    def revoked(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject FROM revocations ORDER BY subject"
            ).fetchall()
        return [r[0] for r in rows]


class CachedRevocationList:
    """A fast, TTL-refreshed in-memory view over a shared revocation source, for
    the verify hot path. ``is_revoked`` is an O(1) set check; the set reloads
    from the source every ``ttl_seconds``. A ``revoke``/``restore`` through this
    wrapper writes through to the source AND updates the local set immediately,
    so the acting replica sees it at once and the others within the TTL."""

    def __init__(
        self,
        source: RevocationStore,
        *,
        ttl_seconds: float = 5.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._source = source
        self._ttl = ttl_seconds
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._cache: set[str] = set()
        self._loaded_at = 0.0
        self._primed = False

    def _refresh_if_stale(self) -> None:
        with self._lock:
            if self._primed and self._now() - self._loaded_at < self._ttl:
                return
        # Read the source outside the lock (a network/db call); swap under it.
        # Concurrent refreshes are harmless (idempotent) and eventually consistent.
        fresh = set(self._source.revoked())
        with self._lock:
            self._cache = fresh
            self._loaded_at = self._now()
            self._primed = True

    def is_revoked(self, subject: str) -> bool:
        self._refresh_if_stale()
        with self._lock:
            return subject in self._cache

    def revoke(self, subject: str, *, reason: str = "") -> None:
        self._source.revoke(subject, reason=reason)
        with self._lock:
            self._cache.add(subject)  # immediate on this replica

    def restore(self, subject: str) -> None:
        self._source.restore(subject)
        with self._lock:
            self._cache.discard(subject)

    def revoked(self) -> list[str]:
        self._refresh_if_stale()
        with self._lock:
            return sorted(self._cache)
