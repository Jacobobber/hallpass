"""Postgres backend integration tests. Skipped unless HALLPASS_TEST_DATABASE_URL
points at a Postgres (so the default suite stays green without a database); run
them with that env set to validate the backends against a real engine. They
exercise the same protocols as the SQLite defaults, plus the one thing only a
real Postgres proves: exactly-once claim under FOR UPDATE SKIP LOCKED across
concurrent connections."""

import os
import threading

import pytest

DSN = os.environ.get("HALLPASS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set HALLPASS_TEST_DATABASE_URL to run the Postgres integration tests",
)


def _reset(*tables: str) -> None:
    """Drop the named tables so each test starts clean (the backend recreates
    them on construction)."""
    import psycopg

    with psycopg.connect(DSN) as conn:
        for t in tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()


# -- task queue (the SKIP LOCKED store) ------------------------------------


def test_pg_queue_roundtrip():
    from hallpass import PostgresTaskQueueBackend, TaskQueue

    _reset("tasks")
    q = TaskQueue(backend=PostgresTaskQueueBackend(DSN))
    tid = q.enqueue("resize", args={"w": "640"}, note="img")
    task = q.claim("w1")
    assert task.id == tid and task.do == "resize" and task.args == {"w": "640"}
    assert q.complete(tid, worker="w1", ok=True, fields={"status": "done"})
    assert q.complete(tid, ok=True, fields={"x": "y"}) is False  # idempotent
    res = q.result(tid)
    assert res.ok and res.fields == {"status": "done"} and res.worker == "w1"


def test_pg_queue_fifo_and_outstanding():
    from hallpass import PostgresTaskQueueBackend, TaskQueue

    _reset("tasks")
    q = TaskQueue(backend=PostgresTaskQueueBackend(DSN))
    a = q.enqueue("op", args={"n": "1"})
    b = q.enqueue("op", args={"n": "2"})
    assert q.claim("w").id == a  # oldest first
    assert set(q.outstanding()) == {a, b}
    q.complete(a, ok=True)
    assert q.outstanding() == [b]


def test_pg_queue_expired_lease_reclaimable():
    from hallpass import PostgresTaskQueueBackend, TaskQueue

    _reset("tasks")
    clock = {"t": 1000.0}
    q = TaskQueue(backend=PostgresTaskQueueBackend(DSN, now=lambda: clock["t"]))
    tid = q.enqueue("op")
    q.claim("dead", lease_seconds=60.0)
    assert q.claim("w2", lease_seconds=60.0) is None  # still leased
    clock["t"] += 61.0
    assert q.claim("w2", lease_seconds=60.0).id == tid  # lease lapsed -> reclaimable


def test_pg_queue_exactly_once_under_skip_locked():
    """The payoff: 8 workers, each on its own connection, drain a backlog via
    FOR UPDATE SKIP LOCKED with no task claimed twice or lost."""
    from hallpass import PostgresTaskQueueBackend, TaskQueue

    _reset("tasks")
    q = TaskQueue(backend=PostgresTaskQueueBackend(DSN))
    n = 150
    for i in range(n):
        q.enqueue("op", args={"i": str(i)})

    claimed: list[str] = []
    guard = threading.Lock()

    def worker(name: str) -> None:
        while True:
            task = q.claim(name)
            if task is None:
                if q.pending_count() == 0:
                    return
                continue
            with guard:
                claimed.append(task.id)
            q.complete(task.id, worker=name, ok=True)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(claimed) == n  # every task claimed
    assert len(set(claimed)) == n  # none claimed twice
    assert q.pending_count() == 0


# -- channel policies ------------------------------------------------------


def test_pg_channel_policy_store_shared_across_buses(tmp_path):
    from hallpass import A2ABus, ChannelPolicy, PostgresChannelPolicyStore, Principal

    _reset("a2a_policies")
    policies = PostgresChannelPolicyStore(DSN)
    msg = str(tmp_path / "msgs.db")
    bus_a = A2ABus(path=msg, policies=policies)
    bus_b = A2ABus(path=msg, policies=policies)
    bus_a.declare_channel(
        "build",
        ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"})),
    )
    assert bus_b.channels == ["build"]  # B sees it via shared Postgres
    bus_a.post(Principal("orch", frozenset({"w"})), "build", "task")
    got = bus_b.catch_up(Principal("worker", frozenset({"r"})), "build")
    assert [m.body for m in got] == ["task"]
    bus_a.close()
    bus_b.close()


# -- vault -----------------------------------------------------------------


def test_pg_vault_backend():
    from cryptography.fernet import Fernet

    from hallpass import CredentialVault, PostgresVaultBackend

    _reset("credentials")
    key = Fernet.generate_key()
    vault = CredentialVault(key, backend=PostgresVaultBackend(DSN))
    vault.store("alice", "github", "PLAINTEXT_SECRET")
    assert vault.fetch("alice", "github") == "PLAINTEXT_SECRET"
    assert vault.fetch("bob", "github") is None  # cross-subject isolation
    assert vault.services("alice") == ["github"]
    assert vault.durable is True
    # the backend holds only ciphertext, never the plaintext
    raw = vault._backend.get("alice", "github")
    assert isinstance(raw, bytes) and b"PLAINTEXT_SECRET" not in raw
    # durable across a fresh vault on the same DB
    v2 = CredentialVault(key, backend=PostgresVaultBackend(DSN))
    assert v2.fetch("alice", "github") == "PLAINTEXT_SECRET"
    assert vault.delete("alice", "github") is True
    assert v2.fetch("alice", "github") is None
