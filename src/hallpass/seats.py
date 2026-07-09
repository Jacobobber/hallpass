"""Seats: durable per-(channel, role) membership, the stable counterpart to
soft presence.

Presence (``A2ABus.announce`` / ``roster``) answers "who is live on this channel
right now" — soft state that ages off. A **seat** answers "who holds role R on
channel C" as durable org structure: it survives a restart and changes only by
an explicit ``bind`` / ``unbind`` / rebind, not by a missed heartbeat. So a
fleet has a stable org chart — the reviewer seat on the build channel is held by
one subject until someone rebinds it — layered over the live view presence gives.

One holder per ``(channel, role)``; ``bind`` is self-service rebind (a new
subject taking the seat replaces the previous holder). ``InMemorySeatLedger`` is
the single-process default; ``SqliteSeatLedger`` persists, mirroring the
consent/roles/delegation storage pattern.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

__all__ = ["Seat", "SeatLedger", "InMemorySeatLedger", "SqliteSeatLedger"]


@dataclass(frozen=True)
class Seat:
    """The principal currently holding one role on one channel."""

    channel: str
    role: str
    subject: str
    bound_at: float


class SeatLedger(Protocol):
    def bind(self, channel: str, role: str, subject: str) -> Seat:
        """Seat ``subject`` in ``role`` on ``channel``, replacing any current
        holder (self-service rebind). Returns the new seat."""
        ...

    def holder(self, channel: str, role: str) -> str | None:
        """Who holds ``role`` on ``channel``, or None if the seat is vacant."""
        ...

    def unbind(self, channel: str, role: str) -> bool:
        """Vacate a seat. True if it was held."""
        ...

    def seats(self, channel: str) -> list[Seat]:
        """Every held seat on ``channel``, sorted by role."""
        ...

    def held_by(self, subject: str) -> list[Seat]:
        """Every seat ``subject`` holds, across channels."""
        ...


class InMemorySeatLedger:
    """Single-process seat ledger; thread-safe, not durable."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._seats: dict[tuple[str, str], Seat] = {}
        self._lock = threading.Lock()

    def bind(self, channel: str, role: str, subject: str) -> Seat:
        seat = Seat(channel=channel, role=role, subject=subject, bound_at=self._now())
        with self._lock:
            self._seats[(channel, role)] = seat
        return seat

    def holder(self, channel: str, role: str) -> str | None:
        with self._lock:
            seat = self._seats.get((channel, role))
        return seat.subject if seat else None

    def unbind(self, channel: str, role: str) -> bool:
        with self._lock:
            return self._seats.pop((channel, role), None) is not None

    def seats(self, channel: str) -> list[Seat]:
        with self._lock:
            here = [s for (ch, _), s in self._seats.items() if ch == channel]
        return sorted(here, key=lambda s: s.role)

    def held_by(self, subject: str) -> list[Seat]:
        with self._lock:
            mine = [s for s in self._seats.values() if s.subject == subject]
        return sorted(mine, key=lambda s: (s.channel, s.role))


class SqliteSeatLedger:
    """A durable seat ledger backed by SQLite; seats survive a restart and
    change only by explicit bind/unbind."""

    def __init__(
        self, *, path: str = ":memory:", now: Callable[[], float] = time.time
    ) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS seats ("
                " channel TEXT NOT NULL, role TEXT NOT NULL,"
                " subject TEXT NOT NULL, bound_at REAL NOT NULL,"
                " PRIMARY KEY (channel, role))"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seats_subject ON seats(subject)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def bind(self, channel: str, role: str, subject: str) -> Seat:
        at = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO seats (channel, role, subject, bound_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(channel, role) DO UPDATE SET"
                " subject = excluded.subject, bound_at = excluded.bound_at",
                (channel, role, subject, at),
            )
        return Seat(channel=channel, role=role, subject=subject, bound_at=at)

    def holder(self, channel: str, role: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT subject FROM seats WHERE channel = ? AND role = ?",
                (channel, role),
            ).fetchone()
        return row[0] if row else None

    def unbind(self, channel: str, role: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM seats WHERE channel = ? AND role = ?", (channel, role)
            )
            return cur.rowcount > 0

    def seats(self, channel: str) -> list[Seat]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, subject, bound_at FROM seats"
                " WHERE channel = ? ORDER BY role",
                (channel,),
            ).fetchall()
        return [
            Seat(channel=channel, role=role, subject=subject, bound_at=bound_at)
            for role, subject, bound_at in rows
        ]

    def held_by(self, subject: str) -> list[Seat]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, role, bound_at FROM seats"
                " WHERE subject = ? ORDER BY channel, role",
                (subject,),
            ).fetchall()
        return [
            Seat(channel=channel, role=role, subject=subject, bound_at=bound_at)
            for channel, role, bound_at in rows
        ]
