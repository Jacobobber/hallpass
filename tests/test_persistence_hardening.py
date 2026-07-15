"""SQLite persistence hardening: the hot queries are indexed (and the planner
actually uses the indexes), and WAL is enabled uniformly on the file-backed
stores. Each test pins a property that keeps the substrate from degrading as
tables grow, or that a regression would silently undo."""

import time

from hallpass import (
    A2ABus,
    AuditEvent,
    ChannelPolicy,
    Principal,
    SqliteAuditLog,
    SqlitePendingStore,
    TaskQueue,
)
from hallpass.vault import CredentialVault


def _index_names(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _plan(conn, sql, params=()) -> str:
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(r[-1] for r in rows)


def test_taskqueue_claim_uses_index_not_scan(tmp_path):
    q = TaskQueue(path=str(tmp_path / "q.db"))
    for i in range(500):
        tid = q.enqueue("op", args={"n": str(i)})
        if i % 4 == 0:
            q.claim("w")
            q.complete(tid, ok=True)
    conn = q._backend._conn  # the queue delegates storage to a SQLite backend
    assert "idx_tasks_status_created" in _index_names(conn)
    plan = _plan(
        conn,
        "SELECT id, do, args, note FROM tasks"
        " WHERE status = 'pending' OR (status = 'leased' AND leased_at < ?)"
        " ORDER BY created_at LIMIT 1",
        (time.time(),),
    )
    # The row-finding must go through the index, not scan the (growing) table.
    assert "idx_tasks_status_created" in plan
    assert "SCAN tasks" not in plan
    q.close()


def test_audit_subject_query_uses_index(tmp_path):
    a = SqliteAuditLog(path=str(tmp_path / "a.db"))
    for i in range(500):
        a.record(
            AuditEvent(
                subject=f"u{i % 25}", action="call_tool", decision="allow", tool="t"
            )
        )
    names = _index_names(a._conn)
    assert {"idx_audit_subject", "idx_audit_tool"} <= names
    subj_plan = _plan(
        a._conn,
        "SELECT subject FROM audit WHERE subject = ? ORDER BY id DESC LIMIT ?",
        ("u1", 100),
    )
    assert "idx_audit_subject" in subj_plan and "SCAN audit" not in subj_plan
    tool_plan = _plan(
        a._conn,
        "SELECT subject FROM audit WHERE tool = ? ORDER BY id DESC LIMIT ?",
        ("t", 100),
    )
    assert "idx_audit_tool" in tool_plan and "SCAN audit" not in tool_plan
    a.close()


def test_a2a_roster_uses_index(tmp_path):
    bus = A2ABus(path=str(tmp_path / "b.db"))
    bus.declare_channel("c", ChannelPolicy())
    for i in range(200):
        bus.announce(Principal(f"a{i}", frozenset()), "c")
    # the bus delegates its message/cursor/presence storage to an A2AStore
    assert "idx_a2a_presence_channel_seen" in _index_names(bus._store._conn)
    plan = _plan(
        bus._store._conn,
        "SELECT subject FROM a2a_presence WHERE channel = ? AND last_seen >= ?"
        " ORDER BY subject",
        ("c", 0.0),
    )
    assert "idx_a2a_presence_channel_seen" in plan
    assert "SCAN a2a_presence" not in plan
    bus.close()


def _journal_mode(conn) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()


def test_wal_enabled_on_file_backed_stores(tmp_path):
    """Every file-backed SQLite store runs in WAL so readers don't block the
    writer. (WAL is a no-op on :memory:, which is why this uses files.)"""
    from cryptography.fernet import Fernet

    vault = CredentialVault(Fernet.generate_key(), path=str(tmp_path / "v.db"))
    queue = TaskQueue(path=str(tmp_path / "tq.db"))
    stores = [
        SqliteAuditLog(path=str(tmp_path / "au.db")),
        SqlitePendingStore(path=str(tmp_path / "pend.db")),
        # the vault, queue, and bus delegate storage to a backend; the conn
        # lives there
        vault._backend,
        queue._backend,
        A2ABus(path=str(tmp_path / "a2a.db"))._store,
    ]
    try:
        for s in stores:
            assert _journal_mode(s._conn) == "wal", type(s).__name__
    finally:
        for s in stores:
            s.close()
