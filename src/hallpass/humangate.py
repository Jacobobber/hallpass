"""Human gates: actions a service principal may never decide alone.

Some envelope changes must stay a human's call -- granting or widening a
capability, approving an irreversible or outward-facing action, onboarding or
offboarding an identity, key custody, a break-glass override. A human gate makes
that structural: the action is held ``pending`` until a principal records a
decision, and ``decide`` refuses a **service** principal (``HumanGateError``).
Only a human (a non-service ``Principal``) can clear it, and the gate records who
did, so the decision is attributable in the audit trail.

The unifying rule of the governance layer: agents propose and execute within a
granted envelope; humans define and change the envelope -- and every envelope
change is a human gate. ``InMemoryHumanGateLedger`` is the single-process
default; ``SqliteHumanGateLedger`` persists, so a gate opened before a restart is
still pending after it (a pending decision is never silently lost).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .identity import Principal

__all__ = [
    "Gate",
    "HumanGateError",
    "HumanGateLedger",
    "InMemoryHumanGateLedger",
    "SqliteHumanGateLedger",
]

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


class HumanGateError(Exception):
    """A service principal tried to decide a human gate, or a gate was decided
    twice. Only a human may clear a human gate, and only once."""


@dataclass(frozen=True)
class Gate:
    """An action awaiting -- or carrying -- a human decision."""

    id: str
    reason: str
    status: str = PENDING
    decided_by: str | None = None
    decided_at: float | None = None
    note: str = ""

    @property
    def cleared(self) -> bool:
        return self.status == APPROVED


class HumanGateLedger(Protocol):
    def require(self, gate_id: str, *, reason: str = "") -> Gate:
        """Open a gate that needs a human decision (status ``pending``).
        Idempotent for a still-pending gate."""
        ...

    def decide(
        self, gate_id: str, principal: Principal, *, approved: bool, note: str = ""
    ) -> Gate:
        """Record a human's decision. Raises ``HumanGateError`` if ``principal``
        is a service (an agent can never clear a human gate) or the gate was
        already decided; ``KeyError`` if the gate was never opened."""
        ...

    def get(self, gate_id: str) -> Gate | None: ...

    def pending(self) -> list[str]:
        """Gate ids still awaiting a human decision, sorted."""
        ...

    def cleared(self, gate_id: str) -> bool:
        """Whether the gate was approved by a human."""
        ...


def _decide(
    gate: Gate, principal: Principal, approved: bool, note: str, at: float
) -> Gate:
    if principal.is_service:
        raise HumanGateError(
            f"service principal {principal.subject!r} may not decide human gate "
            f"{gate.id!r}: only a human (non-service) principal can clear it"
        )
    if gate.status != PENDING:
        raise HumanGateError(
            f"human gate {gate.id!r} was already decided ({gate.status}); a "
            "decision is final"
        )
    return Gate(
        id=gate.id,
        reason=gate.reason,
        status=APPROVED if approved else DENIED,
        decided_by=principal.subject,
        decided_at=at,
        note=note,
    )


class InMemoryHumanGateLedger:
    """Single-process human-gate ledger; thread-safe, not durable."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._gates: dict[str, Gate] = {}
        self._lock = threading.Lock()

    def require(self, gate_id: str, *, reason: str = "") -> Gate:
        with self._lock:
            existing = self._gates.get(gate_id)
            if existing is not None and existing.status == PENDING:
                return existing
            gate = Gate(id=gate_id, reason=reason, status=PENDING)
            self._gates[gate_id] = gate
            return gate

    def decide(
        self, gate_id: str, principal: Principal, *, approved: bool, note: str = ""
    ) -> Gate:
        with self._lock:
            gate = self._gates.get(gate_id)
            if gate is None:
                raise KeyError(
                    f"no human gate {gate_id!r} is open; call require() first"
                )
            decided = _decide(gate, principal, approved, note, self._now())
            self._gates[gate_id] = decided
            return decided

    def get(self, gate_id: str) -> Gate | None:
        with self._lock:
            return self._gates.get(gate_id)

    def pending(self) -> list[str]:
        with self._lock:
            return sorted(g.id for g in self._gates.values() if g.status == PENDING)

    def cleared(self, gate_id: str) -> bool:
        with self._lock:
            gate = self._gates.get(gate_id)
        return gate is not None and gate.cleared


class SqliteHumanGateLedger:
    """A durable human-gate ledger backed by SQLite; a gate opened before a
    restart is still pending after it, and decisions are queryable."""

    def __init__(
        self, *, path: str = ":memory:", now: Callable[[], float] = time.time
    ) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS human_gates ("
                " id TEXT PRIMARY KEY, reason TEXT NOT NULL, status TEXT NOT NULL,"
                " decided_by TEXT, decided_at REAL, note TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_human_gates_status"
                " ON human_gates(status)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row(self, gate_id: str) -> Gate | None:
        row = self._conn.execute(
            "SELECT id, reason, status, decided_by, decided_at, note"
            " FROM human_gates WHERE id = ?",
            (gate_id,),
        ).fetchone()
        if row is None:
            return None
        return Gate(
            id=row[0],
            reason=row[1],
            status=row[2],
            decided_by=row[3],
            decided_at=row[4],
            note=row[5],
        )

    def require(self, gate_id: str, *, reason: str = "") -> Gate:
        with self._lock, self._conn:
            existing = self._row(gate_id)
            if existing is not None and existing.status == PENDING:
                return existing
            self._conn.execute(
                "INSERT INTO human_gates (id, reason, status, note)"
                " VALUES (?, ?, ?, '')"
                " ON CONFLICT(id) DO UPDATE SET reason = excluded.reason,"
                " status = 'pending', decided_by = NULL, decided_at = NULL, note = ''",
                (gate_id, reason, PENDING),
            )
            return Gate(id=gate_id, reason=reason, status=PENDING)

    def decide(
        self, gate_id: str, principal: Principal, *, approved: bool, note: str = ""
    ) -> Gate:
        with self._lock, self._conn:
            gate = self._row(gate_id)
            if gate is None:
                raise KeyError(
                    f"no human gate {gate_id!r} is open; call require() first"
                )
            decided = _decide(gate, principal, approved, note, self._now())
            self._conn.execute(
                "UPDATE human_gates SET status = ?, decided_by = ?, decided_at = ?,"
                " note = ? WHERE id = ?",
                (decided.status, decided.decided_by, decided.decided_at, note, gate_id),
            )
            return decided

    def get(self, gate_id: str) -> Gate | None:
        with self._lock:
            return self._row(gate_id)

    def pending(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM human_gates WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [r[0] for r in rows]

    def cleared(self, gate_id: str) -> bool:
        with self._lock:
            gate = self._row(gate_id)
        return gate is not None and gate.cleared
