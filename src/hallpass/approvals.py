"""Separation of duties: an author cannot approve its own work.

The single most important governance rule for a fleet that can act: the
principal that produced an artifact is never the one that approves it. hallpass
enforces it two ways, both in the same scope vocabulary as everything else:

- **At approval time** (`ApprovalLedger`): recording an approval refuses if the
  approver is the artifact's author (`ApprovalError`), and counts *distinct*
  approvers, so ``approved(artifact, min_approvals=2)`` means two different
  principals signed off.
- **At provisioning time** (`separation_of_duties`): a pure check over a scope
  set for any artifact the set holds *both* ``author:<X>`` and ``approve:<X>``
  for -- refuse such a role, harness preset, or minted token and no principal
  can ever be in a position to approve its own work.

``InMemoryApprovalLedger`` is the single-process default; ``SqliteApprovalLedger``
persists (the approval trail should outlive a restart), mirroring the
consent/roles/delegation storage pattern.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Protocol

__all__ = [
    "Approval",
    "ApprovalError",
    "ApprovalLedger",
    "InMemoryApprovalLedger",
    "SqliteApprovalLedger",
    "separation_of_duties",
]


class ApprovalError(Exception):
    """A principal tried to approve an artifact it authored. The author is
    never the approver -- that is the whole point of a second sign-off."""


@dataclass(frozen=True)
class Approval:
    """One principal's sign-off on one artifact."""

    artifact: str
    approver: str
    approved_at: float
    note: str = ""


def separation_of_duties(
    scopes: Iterable[str],
    *,
    author_prefix: str = "author:",
    approve_prefix: str = "approve:",
) -> frozenset[str]:
    """The artifacts for which ``scopes`` holds BOTH author and approve
    authority -- a separation-of-duties conflict. Empty means no conflict.
    Refuse a role / harness preset / minted token whose scopes return a
    non-empty set, so one principal can never approve its own work."""
    s = set(scopes)
    authored = {x[len(author_prefix) :] for x in s if x.startswith(author_prefix)}
    approving = {x[len(approve_prefix) :] for x in s if x.startswith(approve_prefix)}
    return frozenset(authored & approving)


class ApprovalLedger(Protocol):
    def record(
        self, artifact: str, approver: str, *, author: str, note: str = ""
    ) -> Approval:
        """Record ``approver``'s sign-off on ``artifact``. Raises
        ``ApprovalError`` if the approver is the author (no self-approval).
        Idempotent per (artifact, approver)."""
        ...

    def approvers(self, artifact: str) -> list[str]:
        """The distinct principals who have approved ``artifact``, sorted."""
        ...

    def approvals(self, artifact: str) -> list[Approval]:
        """Every sign-off on ``artifact``, sorted by approver."""
        ...

    def approved(self, artifact: str, *, min_approvals: int = 1) -> bool:
        """Whether ``artifact`` has at least ``min_approvals`` distinct
        non-author approvals."""
        ...


class InMemoryApprovalLedger:
    """Single-process approval ledger; thread-safe, not durable."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._by_artifact: dict[str, dict[str, Approval]] = {}
        self._lock = threading.Lock()

    def record(
        self, artifact: str, approver: str, *, author: str, note: str = ""
    ) -> Approval:
        if approver == author:
            raise ApprovalError(
                f"{approver!r} cannot approve artifact {artifact!r}: it authored it "
                "(approval requires a distinct principal)"
            )
        approval = Approval(
            artifact=artifact, approver=approver, approved_at=self._now(), note=note
        )
        with self._lock:
            self._by_artifact.setdefault(artifact, {})[approver] = approval
        return approval

    def approvers(self, artifact: str) -> list[str]:
        with self._lock:
            return sorted(self._by_artifact.get(artifact, {}))

    def approvals(self, artifact: str) -> list[Approval]:
        with self._lock:
            here = list(self._by_artifact.get(artifact, {}).values())
        return sorted(here, key=lambda a: a.approver)

    def approved(self, artifact: str, *, min_approvals: int = 1) -> bool:
        with self._lock:
            return len(self._by_artifact.get(artifact, {})) >= min_approvals


class SqliteApprovalLedger:
    """A durable approval ledger backed by SQLite; the sign-off trail survives
    a restart and is queryable after the fact."""

    def __init__(
        self, *, path: str = ":memory:", now: Callable[[], float] = time.time
    ) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS approvals ("
                " artifact TEXT NOT NULL, approver TEXT NOT NULL,"
                " approved_at REAL NOT NULL, note TEXT NOT NULL,"
                " PRIMARY KEY (artifact, approver))"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(
        self, artifact: str, approver: str, *, author: str, note: str = ""
    ) -> Approval:
        if approver == author:
            raise ApprovalError(
                f"{approver!r} cannot approve artifact {artifact!r}: it authored it "
                "(approval requires a distinct principal)"
            )
        at = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (artifact, approver, approved_at, note)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(artifact, approver) DO UPDATE SET"
                " approved_at = excluded.approved_at, note = excluded.note",
                (artifact, approver, at, note),
            )
        return Approval(artifact=artifact, approver=approver, approved_at=at, note=note)

    def approvers(self, artifact: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approver FROM approvals WHERE artifact = ? ORDER BY approver",
                (artifact,),
            ).fetchall()
        return [r[0] for r in rows]

    def approvals(self, artifact: str) -> list[Approval]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approver, approved_at, note FROM approvals"
                " WHERE artifact = ? ORDER BY approver",
                (artifact,),
            ).fetchall()
        return [
            Approval(
                artifact=artifact, approver=approver, approved_at=approved_at, note=note
            )
            for approver, approved_at, note in rows
        ]

    def approved(self, artifact: str, *, min_approvals: int = 1) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE artifact = ?", (artifact,)
            ).fetchone()
        return int(row[0]) >= min_approvals
