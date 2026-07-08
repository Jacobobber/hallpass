"""An audit trail for every authorization decision.

An auth boundary that does not record its decisions cannot be reviewed
after an incident. hallpass records every list and call: who, what,
allowed or denied, and why.

The rule learned the hard way on a production bridge: DENIALS must be
audited too, not just successes. A refused call is exactly the event a
security review needs, and the easy mistake is to instrument only the
happy path so denials leave no trace. Here every path emits an event.

Records carry the subject, the tool name, the decision, and an opaque
reason. They never carry the token, a claim value, or a credential -- an
audit log is a log, and logs leak.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["AuditEvent", "AuditSink", "InMemoryAuditLog", "SqliteAuditLog"]

# Subject placeholder for a call whose token never verified, so there is
# no authenticated identity to attribute the (denied) attempt to.
UNVERIFIED = "<unverified>"


@dataclass(frozen=True)
class AuditEvent:
    subject: str
    action: str  # "list_tools" | "call_tool"
    decision: str  # "allow" | "deny"
    tool: str | None = None
    reason: str = ""
    at: float = field(default_factory=time.time)
    # Handler wall-clock for a completed call_tool, in milliseconds; None for
    # everything else. Lets the audit trail double as a latency source.
    duration_ms: float | None = None


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None:
        """Persist one audit event. Called on every decision, allow or
        deny; must not raise on well-formed input."""
        ...


class InMemoryAuditLog:
    """A bounded, thread-safe sink for tests and small deployments; keeps
    the most recent ``capacity`` events. Production wires its own sink
    (a file, a table, an event bus) behind the same protocol."""

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._capacity:
                del self._events[0]

    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)


class SqliteAuditLog:
    """A durable, queryable audit sink backed by SQLite. Records every event
    and answers "what did user X do", "what got denied", "which calls were
    slow" via ``query``. Pass a file ``path`` to keep the trail across
    restarts; ``:memory:`` is the single-process default. Mirrors the vault/A2A
    storage pattern."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " subject TEXT NOT NULL, action TEXT NOT NULL,"
                " decision TEXT NOT NULL, tool TEXT, reason TEXT NOT NULL,"
                " at REAL NOT NULL, duration_ms REAL)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(self, event: AuditEvent) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit"
                " (subject, action, decision, tool, reason, at, duration_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.subject,
                    event.action,
                    event.decision,
                    event.tool,
                    event.reason,
                    event.at,
                    event.duration_ms,
                ),
            )

    def query(
        self,
        *,
        subject: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Return matching events, newest first. Every filter is optional and
        ANDed; ``since`` is a lower bound on ``at`` (Unix seconds)."""
        clauses = []
        params: list[object] = []
        for column, value in (
            ("subject", subject),
            ("tool", tool),
            ("decision", decision),
            ("action", action),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(limit, 0))
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject, action, decision, tool, reason, at, duration_ms"
                f" FROM audit{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            AuditEvent(
                subject=r[0],
                action=r[1],
                decision=r[2],
                tool=r[3],
                reason=r[4],
                at=r[5],
                duration_ms=r[6],
            )
            for r in rows
        ]
