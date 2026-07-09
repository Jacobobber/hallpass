"""A record of what each user has connected, so it can be shown and revoked.

The vault holds the secret; this holds the *fact* of the grant -- which
service, which scopes, and when. A multi-user server needs both: "here is
everything you have connected, revoke any of it" is table stakes for handing
your credentials to someone else's software, and it is not something the raw
credential store answers (it deliberately knows only ciphertext).

The ledger is injected behind ``ConsentLedger``. ``InMemoryConsentLedger`` is
the single-process default (thread-safe, but lost on restart);
``SqliteConsentLedger`` persists the same records to a file, so a grant survives
a restart and a revoke is durable -- the durable backing the in-memory one only
described. Both share the vault/A2A/queue SQLite pattern (one connection, WAL,
one lock), so a real deployment can back consent with the same database as the
rest of its state.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "Consent",
    "ConsentLedger",
    "InMemoryConsentLedger",
    "SqliteConsentLedger",
]


@dataclass(frozen=True)
class Consent:
    """One user's active grant for one service."""

    subject: str
    service: str
    scopes: tuple[str, ...]
    granted_at: float


class ConsentLedger(Protocol):
    def grant(
        self, subject: str, service: str, scopes: Iterable[str], *, at: float
    ) -> None:
        """Record (or replace) the user's consent for a service."""
        ...

    def get(self, subject: str, service: str) -> Consent | None: ...

    def list(self, subject: str) -> list[Consent]:
        """Every service this user has an active consent for."""
        ...

    def revoke(self, subject: str, service: str) -> bool:
        """Remove the consent record. True if one existed."""
        ...


class InMemoryConsentLedger:
    """Single-process consent ledger. Thread-safe (a lock guards the map, since
    the reference HTTP server is threaded), but not durable -- records are lost
    on restart. Use ``SqliteConsentLedger`` for a grant that survives a restart,
    behind the same protocol."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], Consent] = {}
        self._lock = threading.Lock()

    def grant(
        self, subject: str, service: str, scopes: Iterable[str], *, at: float
    ) -> None:
        record = Consent(
            subject=subject, service=service, scopes=tuple(scopes), granted_at=at
        )
        with self._lock:
            self._records[(subject, service)] = record

    def get(self, subject: str, service: str) -> Consent | None:
        with self._lock:
            return self._records.get((subject, service))

    def list(self, subject: str) -> list[Consent]:
        with self._lock:
            records = [c for (s, _), c in self._records.items() if s == subject]
        return sorted(records, key=lambda c: c.service)

    def revoke(self, subject: str, service: str) -> bool:
        with self._lock:
            return self._records.pop((subject, service), None) is not None


class SqliteConsentLedger:
    """A durable consent ledger backed by SQLite, so grants survive a restart
    and a revoke is persistent. Pass a file ``path`` to persist and to share
    across instances; ``:memory:`` is a single-process fallback. Mirrors the
    vault/A2A/queue storage pattern (one connection, WAL, one lock)."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # WAL uniformly across the SQLite-backed stores (no-op on :memory:).
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS consent ("
                " subject TEXT NOT NULL, service TEXT NOT NULL,"
                " scopes TEXT NOT NULL, granted_at REAL NOT NULL,"
                " PRIMARY KEY (subject, service))"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def grant(
        self, subject: str, service: str, scopes: Iterable[str], *, at: float
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO consent (subject, service, scopes, granted_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subject, service) DO UPDATE"
                " SET scopes = excluded.scopes, granted_at = excluded.granted_at",
                (subject, service, " ".join(scopes), at),
            )

    def get(self, subject: str, service: str) -> Consent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT scopes, granted_at FROM consent"
                " WHERE subject = ? AND service = ?",
                (subject, service),
            ).fetchone()
        if row is None:
            return None
        return Consent(
            subject=subject,
            service=service,
            scopes=tuple(row[0].split()) if row[0] else (),
            granted_at=row[1],
        )

    def list(self, subject: str) -> list[Consent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT service, scopes, granted_at FROM consent"
                " WHERE subject = ? ORDER BY service",
                (subject,),
            ).fetchall()
        return [
            Consent(
                subject=subject,
                service=r[0],
                scopes=tuple(r[1].split()) if r[1] else (),
                granted_at=r[2],
            )
            for r in rows
        ]

    def revoke(self, subject: str, service: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM consent WHERE subject = ? AND service = ?",
                (subject, service),
            )
            return cur.rowcount > 0
