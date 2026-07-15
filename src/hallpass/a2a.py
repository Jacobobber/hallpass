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

*Where* messages, cursors, and presence live is an ``A2AStore``: SQLite by
default (``SqliteA2AStore``, durable across processes on one host),
in-memory for tests (``InMemoryA2AStore``), or a shared database for a
multi-replica fleet. The store keeps two properties however it likes -- a
monotonic per-channel sequence and a forward-only cursor -- while the bus
keeps the authorization, auditing, and read-time sanitization above it.
Channel *policies* are a separate ``ChannelPolicyStore`` for the same
reason: authorization must be identical across replicas even when the
message log is not shared.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from .audit import AuditEvent, AuditSink
from .identity import Principal
from .sanitize import sanitize

__all__ = [
    "ChannelPolicy",
    "ChannelDenied",
    "ChannelPolicyStore",
    "InMemoryChannelPolicyStore",
    "SqliteChannelPolicyStore",
    "A2AMessage",
    "A2AStore",
    "InMemoryA2AStore",
    "SqliteA2AStore",
    "A2ABus",
]


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

-- roster() filters a channel's rows by last_seen; the PK's leading column is
-- the channel, but the recency bound is not covered by it, so key both.
CREATE INDEX IF NOT EXISTS idx_a2a_presence_channel_seen
    ON a2a_presence(channel, last_seen);
"""


class ChannelPolicyStore(Protocol):
    """Where a bus keeps its channel policies. The default is per-process
    (`InMemoryChannelPolicyStore`); a shared one (`SqliteChannelPolicyStore` or
    another backend) keeps channel authorization identical across replicas,
    instead of each replica having to re-declare every channel and 404ing on any
    it hasn't."""

    def declare(self, channel: str, policy: ChannelPolicy) -> None: ...
    def get(self, channel: str) -> ChannelPolicy | None: ...
    def channels(self) -> list[str]: ...


class InMemoryChannelPolicyStore:
    """Per-process channel policies (the bus default). Thread-safe, not shared:
    behind a load balancer each replica needs the same channels declared."""

    def __init__(self) -> None:
        self._policies: dict[str, ChannelPolicy] = {}
        self._lock = threading.Lock()

    def declare(self, channel: str, policy: ChannelPolicy) -> None:
        with self._lock:
            self._policies[channel] = policy

    def get(self, channel: str) -> ChannelPolicy | None:
        with self._lock:
            return self._policies.get(channel)

    def channels(self) -> list[str]:
        with self._lock:
            return sorted(self._policies)


class SqliteChannelPolicyStore:
    """Durable, shared channel policies backed by SQLite, so a channel declared
    once is visible to every bus that points at the same store -- channel
    authorization no longer diverges across replicas. Pass a file ``path`` to
    share; ``:memory:`` is a single-process fallback."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS a2a_policies ("
                " channel TEXT PRIMARY KEY, post_scopes TEXT NOT NULL,"
                " read_scopes TEXT NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def declare(self, channel: str, policy: ChannelPolicy) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO a2a_policies (channel, post_scopes, read_scopes)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(channel) DO UPDATE SET"
                " post_scopes = excluded.post_scopes,"
                " read_scopes = excluded.read_scopes",
                (
                    channel,
                    " ".join(sorted(policy.post_scopes)),
                    " ".join(sorted(policy.read_scopes)),
                ),
            )

    def get(self, channel: str) -> ChannelPolicy | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT post_scopes, read_scopes FROM a2a_policies WHERE channel = ?",
                (channel,),
            ).fetchone()
        if row is None:
            return None
        return ChannelPolicy(
            post_scopes=frozenset(row[0].split()),
            read_scopes=frozenset(row[1].split()),
        )

    def channels(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel FROM a2a_policies ORDER BY channel"
            ).fetchall()
        return [r[0] for r in rows]


# A stored message as the backend hands it back: (seq, sender, body, created_at).
StoredMessage = tuple[int, str, str, float]


class A2AStore(Protocol):
    """Storage for the message log, read cursors, and presence -- the durable
    part of the bus. Each backend keeps the same two guarantees its own way:

    - ``append`` assigns a **monotonic, gap-free per-channel sequence** under
      whatever serialization the engine gives it (SQLite: ``BEGIN IMMEDIATE``;
      Postgres: a per-channel advisory lock), so two concurrent posts never
      collide on a seq.
    - ``advance_cursor`` is **forward-only** (``MAX(current, up_to)``), so a
      stale ack cannot regress a reader's position.

    The bus layers authorization, auditing, and read-time sanitization on top;
    the store holds raw bytes and never inspects scopes."""

    def append(self, channel: str, sender: str, body: str, created_at: float) -> int:
        """Append a message and return its newly assigned per-channel seq."""
        ...

    def read_after(
        self, channel: str, after_seq: int, limit: int
    ) -> list[StoredMessage]:
        """Messages with ``seq > after_seq``, ascending, at most ``limit``."""
        ...

    def head(self, channel: str) -> int:
        """The greatest seq on the channel, or 0 if it has no messages."""
        ...

    def cursor(self, subject: str, channel: str) -> int:
        """The subject's acked position on the channel, 0 if never acked."""
        ...

    def advance_cursor(self, subject: str, channel: str, up_to: int) -> int:
        """Move the cursor forward-only to ``max(current, up_to)``; return it."""
        ...

    def touch_presence(self, channel: str, subject: str, at: float) -> None:
        """Record ``subject`` as live on ``channel`` at time ``at`` (upsert)."""
        ...

    def roster(self, channel: str, since: float) -> list[str]:
        """Subjects whose last_seen is ``>= since``, sorted."""
        ...

    def close(self) -> None: ...


class InMemoryA2AStore:
    """Process-local message/cursor/presence storage; thread-safe (the lock is
    what makes ``append`` assign a unique seq across threads), not durable. The
    default when a bus is constructed with no path and no store is still SQLite
    ``:memory:``; this is for tests and for embedding without a filesystem."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._msgs: dict[str, list[StoredMessage]] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._presence: dict[tuple[str, str], float] = {}

    def append(self, channel: str, sender: str, body: str, created_at: float) -> int:
        with self._lock:
            log = self._msgs.setdefault(channel, [])
            seq = len(log) + 1  # contiguous per channel
            log.append((seq, sender, body, created_at))
            return seq

    def read_after(
        self, channel: str, after_seq: int, limit: int
    ) -> list[StoredMessage]:
        with self._lock:
            log = self._msgs.get(channel, [])
            return [m for m in log if m[0] > after_seq][:limit]

    def head(self, channel: str) -> int:
        with self._lock:
            log = self._msgs.get(channel)
            return log[-1][0] if log else 0

    def cursor(self, subject: str, channel: str) -> int:
        with self._lock:
            return self._cursors.get((subject, channel), 0)

    def advance_cursor(self, subject: str, channel: str, up_to: int) -> int:
        with self._lock:
            new = max(self._cursors.get((subject, channel), 0), up_to)
            self._cursors[(subject, channel)] = new
            return new

    def touch_presence(self, channel: str, subject: str, at: float) -> None:
        with self._lock:
            self._presence[(channel, subject)] = at

    def roster(self, channel: str, since: float) -> list[str]:
        with self._lock:
            return sorted(
                subject
                for (ch, subject), seen in self._presence.items()
                if ch == channel and seen >= since
            )

    def close(self) -> None:
        pass


class SqliteA2AStore:
    """Durable message/cursor/presence storage on SQLite. ``append`` runs under
    ``BEGIN IMMEDIATE`` (the write lock) so two posters cannot claim the same
    seq. Pass a file ``path`` for durability across processes on one host;
    ``:memory:`` is a single-process default."""

    def __init__(self, *, path: str = ":memory:") -> None:
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

    def append(self, channel: str, sender: str, body: str, created_at: float) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM a2a_messages WHERE channel = ?",
                    (channel,),
                ).fetchone()
                seq = int(row[0]) + 1
                self._conn.execute(
                    "INSERT INTO a2a_messages (channel, seq, sender, body, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (channel, seq, sender, body, created_at),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return seq

    def read_after(
        self, channel: str, after_seq: int, limit: int
    ) -> list[StoredMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, sender, body, created_at FROM a2a_messages"
                " WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
                (channel, after_seq, limit),
            ).fetchall()
        return [(int(s), sender, body, created) for s, sender, body, created in rows]

    def head(self, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM a2a_messages WHERE channel = ?",
                (channel,),
            ).fetchone()
        return int(row[0])

    def cursor(self, subject: str, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT acked_seq FROM a2a_cursors WHERE subject = ? AND channel = ?",
                (subject, channel),
            ).fetchone()
        return int(row[0]) if row else 0

    def advance_cursor(self, subject: str, channel: str, up_to: int) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO a2a_cursors (subject, channel, acked_seq)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(subject, channel) DO UPDATE"
                    " SET acked_seq = MAX(acked_seq, excluded.acked_seq)",
                    (subject, channel, up_to),
                )
                row = self._conn.execute(
                    "SELECT acked_seq FROM a2a_cursors WHERE subject = ? AND channel = ?",
                    (subject, channel),
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return int(row[0])

    def touch_presence(self, channel: str, subject: str, at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO a2a_presence (channel, subject, last_seen)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(channel, subject) DO UPDATE"
                " SET last_seen = excluded.last_seen",
                (channel, subject, at),
            )

    def roster(self, channel: str, since: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject FROM a2a_presence"
                " WHERE channel = ? AND last_seen >= ? ORDER BY subject",
                (channel, since),
            ).fetchall()
        return [str(r[0]) for r in rows]


class A2ABus:
    """Durable, per-principal-authorized agent-to-agent channels.

    Channels are declared with a policy up front; posting and reading are
    authorized against the caller's scopes and audited. Storage is an
    ``A2AStore``: pass a file ``path`` for the durable SQLite default, or a
    ``store`` (e.g. a Postgres one) for a shared multi-replica message log; the
    default ``:memory:`` suits tests. Channel policies are a separate
    ``policies`` store so authorization can be shared even when the log is not.
    """

    def __init__(
        self,
        *,
        path: str = ":memory:",
        audit: AuditSink | None = None,
        sanitize_reads: bool = True,
        policies: ChannelPolicyStore | None = None,
        store: A2AStore | None = None,
    ) -> None:
        # Channel policies default to a per-process store; pass a shared one
        # (SqliteChannelPolicyStore) so authorization is identical across
        # replicas.
        self._policies: ChannelPolicyStore = policies or InMemoryChannelPolicyStore()
        self._audit = audit
        # Bodies are written by other principals; on read they land in a
        # reader's (often a model's) context. Neutralise control/escape
        # spoofing on the way out by default. Storage keeps the raw bytes so
        # an audit or export sees exactly what was sent.
        self._sanitize_reads = sanitize_reads
        # The message log / cursors / presence live in the store; a bare path
        # constructs the SQLite default so existing callers are unaffected.
        self._store: A2AStore = store or SqliteA2AStore(path=path)

    def close(self) -> None:
        self._store.close()

    def declare_channel(self, name: str, policy: ChannelPolicy) -> None:
        """Register a channel and the scopes it requires. Re-declaring
        replaces the policy; existing messages are untouched."""
        self._policies.declare(name, policy)

    @property
    def channels(self) -> list[str]:
        return self._policies.channels()

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
        created = time.time()
        seq = self._store.append(channel, principal.subject, body, created)
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
        position = self._store.cursor(principal.subject, channel)
        while True:
            rows = self._store.read_after(channel, position, page_size)
            if not rows:
                break
            for seq, sender, body, created in rows:
                clean = sanitize(body) if self._sanitize_reads else body
                out.append(A2AMessage(channel, seq, sender, clean, created))
            position = rows[-1][0]
        self._record(principal.subject, "a2a_read", "allow", channel)
        return out

    def ack(self, principal: Principal, channel: str, up_to: int) -> int:
        """Advance this principal's read cursor. Forward-only (a stale ack
        cannot regress it), and it cannot exceed the channel head."""
        policy = self._policies.get(channel)
        need = policy.read_scopes if policy else frozenset()
        self._authorize(principal, channel, need, "a2a_ack")
        # Messages are append-only, so the head only grows: checking it before
        # the forward-only advance can never let the cursor pass the true head.
        if up_to > self._store.head(channel):
            raise ValueError("cannot ack beyond channel head")
        return self._store.advance_cursor(principal.subject, channel, up_to)

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
        self._store.touch_presence(channel, principal.subject, now)
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
        subjects = self._store.roster(channel, cutoff)
        self._record(principal.subject, "a2a_roster", "allow", channel)
        return subjects
