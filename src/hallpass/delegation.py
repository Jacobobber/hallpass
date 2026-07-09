"""Delegation: a bounded, expiring grant of one principal's scopes to another.

Roles say what a principal may do standing. Delegation is the temporary, scoped
hand-off: an orchestrator lets a worker act with *a subset of its own* scopes,
for a while, for a job. Two invariants make it safe governance rather than a
back door:

- **Scope narrowing.** A delegation can only grant scopes the grantor itself
  holds -- ``delegate`` is given the grantor's current scopes and refuses
  (``DelegationError``) to hand out anything beyond them. Authority only ever
  shrinks down a delegation chain, never grows.
- **Expiry.** A delegation carries a TTL; ``active_scopes`` counts only
  unexpired grants, so a temporary hand-off does not become a standing one by
  forgetting to revoke it.

``active_scopes(grantee)`` unions the unexpired scopes delegated to a subject
(across all grantors) -- fold it into what you mint the subject's token with,
alongside its roles. ``InMemoryDelegationLedger`` is the single-process default;
``SqliteDelegationLedger`` persists, mirroring the consent/roles storage pattern.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "Delegation",
    "DelegationError",
    "DelegationLedger",
    "InMemoryDelegationLedger",
    "SqliteDelegationLedger",
]


class DelegationError(ValueError):
    """A delegation tried to grant scopes the grantor does not itself hold.
    Authority narrows down a delegation chain; it never widens."""


@dataclass(frozen=True)
class Delegation:
    """One principal's active, scoped, expiring grant to another."""

    grantor: str
    grantee: str
    scopes: tuple[str, ...]
    granted_at: float
    expires_at: float | None = None
    note: str = ""

    def active_at(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at


def _bounded(scopes: Iterable[str], grantor_scopes: Iterable[str]) -> frozenset[str]:
    wanted = frozenset(scopes)
    over = wanted - frozenset(grantor_scopes)
    if over:
        raise DelegationError(
            f"cannot delegate scopes {sorted(over)}: the grantor does not hold them "
            "(a delegation may only narrow the grantor's own scopes)"
        )
    return wanted


class DelegationLedger(Protocol):
    def delegate(
        self,
        grantor: str,
        grantee: str,
        scopes: Iterable[str],
        *,
        grantor_scopes: Iterable[str],
        ttl_seconds: float | None = None,
        note: str = "",
    ) -> Delegation:
        """Record (or replace) a delegation from ``grantor`` to ``grantee`` for
        ``scopes`` (which must be a subset of ``grantor_scopes``), expiring after
        ``ttl_seconds`` if given. Raises ``DelegationError`` on over-delegation."""
        ...

    def active_scopes(self, grantee: str) -> frozenset[str]:
        """The union of unexpired scopes delegated to ``grantee``, now."""
        ...

    def granted(self, grantee: str) -> list[Delegation]:
        """The unexpired delegations to ``grantee``, sorted by grantor."""
        ...

    def revoke(self, grantor: str, grantee: str) -> bool:
        """Revoke ``grantor``'s delegation to ``grantee``. True if one existed."""
        ...


class InMemoryDelegationLedger:
    """Single-process delegation ledger; thread-safe, not durable."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._records: dict[tuple[str, str], Delegation] = {}
        self._lock = threading.Lock()

    def delegate(
        self,
        grantor: str,
        grantee: str,
        scopes: Iterable[str],
        *,
        grantor_scopes: Iterable[str],
        ttl_seconds: float | None = None,
        note: str = "",
    ) -> Delegation:
        wanted = _bounded(scopes, grantor_scopes)
        now = self._now()
        record = Delegation(
            grantor=grantor,
            grantee=grantee,
            scopes=tuple(sorted(wanted)),
            granted_at=now,
            expires_at=(now + ttl_seconds) if ttl_seconds is not None else None,
            note=note,
        )
        with self._lock:
            self._records[(grantor, grantee)] = record
        return record

    def active_scopes(self, grantee: str) -> frozenset[str]:
        now = self._now()
        out: set[str] = set()
        with self._lock:
            for (_, ge), rec in self._records.items():
                if ge == grantee and rec.active_at(now):
                    out |= set(rec.scopes)
        return frozenset(out)

    def granted(self, grantee: str) -> list[Delegation]:
        now = self._now()
        with self._lock:
            active = [
                rec
                for (_, ge), rec in self._records.items()
                if ge == grantee and rec.active_at(now)
            ]
        return sorted(active, key=lambda d: d.grantor)

    def revoke(self, grantor: str, grantee: str) -> bool:
        with self._lock:
            return self._records.pop((grantor, grantee), None) is not None


class SqliteDelegationLedger:
    """A durable delegation ledger backed by SQLite; delegations survive a
    restart and expire by their stored ``expires_at``."""

    def __init__(
        self, *, path: str = ":memory:", now: Callable[[], float] = time.time
    ) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS delegations ("
                " grantor TEXT NOT NULL, grantee TEXT NOT NULL,"
                " scopes TEXT NOT NULL, granted_at REAL NOT NULL,"
                " expires_at REAL, note TEXT NOT NULL,"
                " PRIMARY KEY (grantor, grantee))"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delegations_grantee"
                " ON delegations(grantee)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def delegate(
        self,
        grantor: str,
        grantee: str,
        scopes: Iterable[str],
        *,
        grantor_scopes: Iterable[str],
        ttl_seconds: float | None = None,
        note: str = "",
    ) -> Delegation:
        wanted = _bounded(scopes, grantor_scopes)
        now = self._now()
        expires_at = (now + ttl_seconds) if ttl_seconds is not None else None
        record = Delegation(
            grantor=grantor,
            grantee=grantee,
            scopes=tuple(sorted(wanted)),
            granted_at=now,
            expires_at=expires_at,
            note=note,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO delegations"
                " (grantor, grantee, scopes, granted_at, expires_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(grantor, grantee) DO UPDATE SET"
                " scopes = excluded.scopes, granted_at = excluded.granted_at,"
                " expires_at = excluded.expires_at, note = excluded.note",
                (grantor, grantee, " ".join(record.scopes), now, expires_at, note),
            )
        return record

    def _active_rows(self, grantee: str) -> list[tuple[str, str, float, float, str]]:
        now = self._now()
        with self._lock:
            return self._conn.execute(
                "SELECT grantor, scopes, granted_at, expires_at, note"
                " FROM delegations"
                " WHERE grantee = ? AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY grantor",
                (grantee, now),
            ).fetchall()

    def active_scopes(self, grantee: str) -> frozenset[str]:
        out: set[str] = set()
        for _, scopes, _, _, _ in self._active_rows(grantee):
            out |= set(scopes.split()) if scopes else set()
        return frozenset(out)

    def granted(self, grantee: str) -> list[Delegation]:
        return [
            Delegation(
                grantor=grantor,
                grantee=grantee,
                scopes=tuple(scopes.split()) if scopes else (),
                granted_at=granted_at,
                expires_at=expires_at,
                note=note,
            )
            for grantor, scopes, granted_at, expires_at, note in self._active_rows(
                grantee
            )
        ]

    def revoke(self, grantor: str, grantee: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM delegations WHERE grantor = ? AND grantee = ?",
                (grantor, grantee),
            )
            return cur.rowcount > 0
