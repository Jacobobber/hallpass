"""Agent-to-agent channels: authenticated, authorized, durable.

hallpass's other layers bridge an agent to tools. This one bridges agents
to each other. It is the same identity and scope model pointed at a
different resource: instead of "which tools may this principal call", it
answers "which channels may this principal post to and read from", and it
delivers messages durably so an agent that dies mid-conversation misses
nothing.

Three ideas are reused rather than reinvented:

- Identity and authorization come from the rest of hallpass. A caller is a
  verified ``Principal`` (a subject plus granted scopes), and a channel's
  ``ChannelPolicy`` says which scopes a principal needs to post and to read.
  Deny is the default: an undeclared channel, or one you lack the scope
  for, is refused with the same opaque message either way, so a caller
  cannot enumerate channels it may not touch.
- Every decision is audited through the same ``AuditSink``, denials
  included.
- Delivery is durable and self-contained: an append-only per-channel log,
  a forward-only ack cursor per (subject, channel), and catch-up on
  reconnect, so a read without an ack means redelivery, never loss.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field

from .audit import AuditEvent, AuditSink
from .identity import Principal
from .sanitize import sanitize

__all__ = ["ChannelPolicy", "ChannelDenied", "A2AMessage", "A2ABus"]


class ChannelDenied(Exception):
    """The principal may not post to or read from this channel, or the
    channel does not exist. The two are deliberately indistinguishable so
    a caller cannot map channels it has no access to."""


@dataclass(frozen=True)
class ChannelPolicy:
    """Scopes a principal must hold to use a channel. Empty means any
    authenticated principal may act; deny is still the default in that a
    channel must be declared before anyone can touch it."""

    post_scopes: frozenset[str] = frozenset()
    read_scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class A2AMessage:
    channel: str
    seq: int
    sender: str  # the posting principal's subject
    body: str
    created_at: float = field(default_factory=time.time)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_messages (
    channel    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    sender     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (channel, seq)
);

CREATE TABLE IF NOT EXISTS a2a_cursors (
    subject   TEXT NOT NULL,
    channel   TEXT NOT NULL,
    acked_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, channel)
);

CREATE TABLE IF NOT EXISTS a2a_presence (
    channel   TEXT NOT NULL,
    subject   TEXT NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (channel, subject)
);
"""


class A2ABus:
    """Durable, per-principal-authorized agent-to-agent channels.

    Channels are declared with a policy up front; posting and reading are
    authorized against the caller's scopes and audited. Pass a file path
    for durability across processes; the default ``:memory:`` suits tests.
    """

    def __init__(
        self,
        *,
        path: str = ":memory:",
        audit: AuditSink | None = None,
        sanitize_reads: bool = True,
    ) -> None:
        self._policies: dict[str, ChannelPolicy] = {}
        self._audit = audit
        # Bodies are written by other principals; on read they land in a
        # reader's (often a model's) context. Neutralise control/escape
        # spoofing on the way out by default. Storage keeps the raw bytes so
        # an audit or export sees exactly what was sent.
        self._sanitize_reads = sanitize_reads
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def declare_channel(self, name: str, policy: ChannelPolicy) -> None:
        """Register a channel and the scopes it requires. Re-declaring
        replaces the policy; existing messages are untouched."""
        with self._lock:
            self._policies[name] = policy

    @property
    def channels(self) -> list[str]:
        with self._lock:
            return sorted(self._policies)

    # -- authorization -----------------------------------------------------

    def _record(
        self, subject: str, action: str, decision: str, channel: str, reason: str = ""
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                AuditEvent(
                    subject=subject,
                    action=action,
                    decision=decision,
                    tool=channel,
                    reason=reason,
                )
            )

    def _authorize(
        self, principal: Principal, channel: str, need: frozenset[str], action: str
    ) -> None:
        # Undeclared channel and insufficient scope both fail closed with
        # the same opaque error, so neither existence nor the guarding scope
        # leaks to a caller who may not act.
        policy = self._policies.get(channel)
        if policy is None or not need <= principal.scopes:
            self._record(
                principal.subject, action, "deny", channel, reason="not_authorized"
            )
            raise ChannelDenied(f"no channel named {channel!r}")

    # -- post / read / ack -------------------------------------------------

    def post(self, principal: Principal, channel: str, body: str) -> A2AMessage:
        policy = self._policies.get(channel)
        need = policy.post_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_post")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM a2a_messages WHERE channel = ?",
                    (channel,),
                ).fetchone()
                seq = int(row[0]) + 1
                created = time.time()
                self._conn.execute(
                    "INSERT INTO a2a_messages (channel, seq, sender, body, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (channel, seq, principal.subject, body, created),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        self._record(principal.subject, "a2a_post", "allow", channel)
        return A2AMessage(channel, seq, principal.subject, body, created)

    def catch_up(
        self, principal: Principal, channel: str, *, page_size: int = 100
    ) -> list[A2AMessage]:
        """Every message on the channel this principal has not yet acked.
        Does NOT ack; the caller acks after handling, so a crash between
        read and ack means redelivery, not loss."""
        policy = self._policies.get(channel)
        need = policy.read_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_read")
        out: list[A2AMessage] = []
        with self._lock:
            cur = self._conn.execute(
                "SELECT acked_seq FROM a2a_cursors WHERE subject = ? AND channel = ?",
                (principal.subject, channel),
            ).fetchone()
            position = int(cur[0]) if cur else 0
            while True:
                rows = self._conn.execute(
                    "SELECT seq, sender, body, created_at FROM a2a_messages"
                    " WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (channel, position, page_size),
                ).fetchall()
                if not rows:
                    break
                for seq, sender, body, created in rows:
                    clean = sanitize(body) if self._sanitize_reads else body
                    out.append(A2AMessage(channel, int(seq), sender, clean, created))
                position = int(rows[-1][0])
        self._record(principal.subject, "a2a_read", "allow", channel)
        return out

    def ack(self, principal: Principal, channel: str, up_to: int) -> int:
        """Advance this principal's read cursor. Forward-only (a stale ack
        cannot regress it), and it cannot exceed the channel head."""
        policy = self._policies.get(channel)
        need = policy.read_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_ack")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                head_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM a2a_messages WHERE channel = ?",
                    (channel,),
                ).fetchone()
                if up_to > int(head_row[0]):
                    raise ValueError("cannot ack beyond channel head")
                self._conn.execute(
                    "INSERT INTO a2a_cursors (subject, channel, acked_seq) VALUES (?, ?, ?)"
                    " ON CONFLICT(subject, channel) DO UPDATE"
                    " SET acked_seq = MAX(acked_seq, excluded.acked_seq)",
                    (principal.subject, channel, up_to),
                )
                new_row = self._conn.execute(
                    "SELECT acked_seq FROM a2a_cursors WHERE subject = ? AND channel = ?",
                    (principal.subject, channel),
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return int(new_row[0])

    # -- presence / live roster --------------------------------------------

    def announce(self, principal: Principal, channel: str) -> float:
        """Record that this principal is live on the channel right now, and
        return the timestamp. Asserting presence is a write, so it needs the
        channel's post scope: a reader that may not post cannot claim a seat.
        Idempotent — re-announcing just refreshes the heartbeat. Call it on a
        timer to stay on the roster."""
        policy = self._policies.get(channel)
        need = policy.post_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_announce")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO a2a_presence (channel, subject, last_seen)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(channel, subject) DO UPDATE"
                " SET last_seen = excluded.last_seen",
                (channel, principal.subject, now),
            )
        self._record(principal.subject, "a2a_announce", "allow", channel)
        return now

    def roster(
        self, principal: Principal, channel: str, *, within: float = 30.0
    ) -> list[str]:
        """The subjects seen live on the channel within the last ``within``
        seconds, sorted. Reading the roster needs the channel's read scope, so
        who-is-here is gated exactly like the messages are. A subject that
        stops heartbeating simply ages off; presence is soft state, never a
        grant."""
        policy = self._policies.get(channel)
        need = policy.read_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_roster")
        cutoff = time.time() - within
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject FROM a2a_presence"
                " WHERE channel = ? AND last_seen >= ? ORDER BY subject",
                (channel, cutoff),
            ).fetchall()
        self._record(principal.subject, "a2a_roster", "allow", channel)
        return [str(r[0]) for r in rows]
