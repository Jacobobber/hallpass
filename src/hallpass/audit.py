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

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["AuditEvent", "AuditSink", "InMemoryAuditLog"]

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
