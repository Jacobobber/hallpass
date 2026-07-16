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


# -- A2A message log -------------------------------------------------------


def _fresh_a2a_store():
    from hallpass import PostgresA2AStore

    _reset("a2a_messages", "a2a_cursors", "a2a_presence")
    return PostgresA2AStore(DSN)


def test_pg_a2a_store_roundtrip():
    store = _fresh_a2a_store()
    assert store.append("build", "orch", "one", 1.0) == 1
    assert store.append("build", "orch", "two", 2.0) == 2
    assert store.append("ops", "orch", "a", 3.0) == 1  # seq is per channel
    assert store.head("build") == 2 and store.head("ops") == 1
    msgs = store.read_after("build", 0, 100)
    assert [(m[0], m[2]) for m in msgs] == [(1, "one"), (2, "two")]
    assert store.read_after("build", 1, 100) == msgs[1:]


def test_pg_a2a_cursor_is_forward_only():
    store = _fresh_a2a_store()
    assert store.cursor("w", "c") == 0
    assert store.advance_cursor("w", "c", 5) == 5
    assert store.advance_cursor("w", "c", 2) == 5  # stale ack cannot regress
    assert store.cursor("w", "c") == 5
    assert store.cursor("other", "c") == 0  # per (subject, channel)


def test_pg_a2a_presence_ages_off():
    store = _fresh_a2a_store()
    store.touch_presence("c", "alice", 100.0)
    store.touch_presence("c", "bob", 100.0)
    assert store.roster("c", 50.0) == ["alice", "bob"]
    assert store.roster("c", 150.0) == []
    store.touch_presence("c", "alice", 200.0)  # refresh
    assert store.roster("c", 150.0) == ["alice"]


def test_pg_a2a_append_monotonic_under_concurrency():
    """The payoff for the shared log: 8 threads, each on its own connection,
    hammer one channel; the per-channel advisory lock must keep the sequence
    monotonic and gap-free (no two posts share a seq, none skipped)."""
    store = _fresh_a2a_store()
    n = 200
    barrier = threading.Barrier(8)
    seqs: list[int] = []
    guard = threading.Lock()

    def poster(base: int) -> None:
        barrier.wait()
        for i in range(n // 8):
            seq = store.append("hot", f"w{base}", f"m{base}-{i}", float(i))
            with guard:
                seqs.append(seq)

    threads = [threading.Thread(target=poster, args=(b,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(seqs) == n
    assert len(set(seqs)) == n  # none collided
    assert sorted(seqs) == list(range(1, n + 1))  # gap-free 1..n
    assert store.head("hot") == n


def test_pg_a2a_shared_message_log_across_buses(tmp_path):
    """Two buses on one Postgres message log see each other's messages and
    share the read cursor -- a stand-in for two replicas on shared storage."""
    from hallpass import A2ABus, ChannelPolicy, PostgresA2AStore, Principal

    _reset("a2a_messages", "a2a_cursors", "a2a_presence")
    policy = ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"}))
    bus_a = A2ABus(store=PostgresA2AStore(DSN))
    bus_b = A2ABus(store=PostgresA2AStore(DSN))
    for bus in (bus_a, bus_b):
        bus.declare_channel("build", policy)  # policies per-bus here
    bus_a.post(Principal("orch", frozenset({"w"})), "build", "task")
    reader = Principal("worker", frozenset({"r"}))
    got = bus_b.catch_up(reader, "build")
    assert [m.body for m in got] == ["task"]
    bus_b.ack(reader, "build", got[-1].seq)
    # the ack is durable in the shared log, so bus_a sees nothing to redeliver
    assert bus_a.catch_up(reader, "build") == []
    bus_a.close()
    bus_b.close()


# -- audit log -------------------------------------------------------------


def test_pg_audit_log_records_and_queries():
    """The audit trail on Postgres: one central, durable record. Newest-first,
    filterable, and it survives a fresh instance (a different replica)."""
    from hallpass import PostgresAuditLog
    from hallpass.audit import AuditEvent

    _reset("audit")
    log = PostgresAuditLog(DSN)
    log.record(
        AuditEvent(
            subject="alice", action="call_tool", decision="allow", tool="ping", at=1.0
        )
    )
    log.record(
        AuditEvent(
            subject="bob",
            action="call_tool",
            decision="deny",
            tool="ping",
            reason="not_authorized",
            at=2.0,
        )
    )
    log.record(
        AuditEvent(subject="alice", action="list_tools", decision="allow", at=3.0)
    )
    # newest first
    allrows = log.query()
    assert [e.at for e in allrows] == [3.0, 2.0, 1.0]
    # filters AND together
    assert [e.subject for e in log.query(subject="alice")] == ["alice", "alice"]
    assert [e.decision for e in log.query(decision="deny")] == ["deny"]
    assert [e.at for e in log.query(since=2.0)] == [3.0, 2.0]
    # durable across a fresh instance (as a second replica would see it)
    log2 = PostgresAuditLog(DSN)
    assert len(log2.query()) == 3


# -- connection pool -------------------------------------------------------


def test_pg_backends_share_one_pool_per_dsn():
    """Backends on the same DSN reuse one connection pool (not a fresh connect
    per operation); the pool hands out distinct connections, so the concurrency
    primitives still hold -- proven by the SKIP-LOCKED and advisory-lock tests
    passing against it."""
    from hallpass import PostgresTaskQueueBackend, PostgresVaultBackend
    from hallpass import postgres_backends as pb

    _reset("tasks", "credentials")
    pb._POOLS.clear()
    q = PostgresTaskQueueBackend(DSN)
    v = PostgresVaultBackend(DSN)
    q.enqueue("op", {}, "")  # force an operation through the pool
    v.services("nobody")
    assert DSN in pb._POOLS  # pooling engaged, not per-op connect
    assert len(pb._POOLS) == 1  # both backends share the one pool for this DSN


# -- revocation ------------------------------------------------------------


def test_pg_revocation_list_shared():
    """The shared revocation source: a revoke is visible to a fresh instance
    (another replica), and CachedRevocationList over it serves the verify path."""
    from hallpass import CachedRevocationList, PostgresRevocationList

    _reset("revocations")
    store = PostgresRevocationList(DSN)
    store.revoke("agent-7", reason="compromised")
    assert store.is_revoked("agent-7") and store.revoked() == ["agent-7"]
    # a second replica's store sees it
    assert PostgresRevocationList(DSN).is_revoked("agent-7")
    # the hot-path cache reads through it
    cached = CachedRevocationList(PostgresRevocationList(DSN), ttl_seconds=5.0)
    assert cached.is_revoked("agent-7")
    store.restore("agent-7")
    assert not PostgresRevocationList(DSN).is_revoked("agent-7")


# -- human gates -----------------------------------------------------------


def test_pg_human_gate_ledger_shared():
    """A gate opened on one replica is pending on all of them; a human clears it
    and the decision is seen fleet-wide; a service principal can never clear it."""
    from hallpass import PostgresHumanGateLedger, Principal

    _reset("human_gates")
    ledger = PostgresHumanGateLedger(DSN)
    ledger.require("deploy-prod", reason="irreversible")
    # a second replica's ledger sees it pending
    assert PostgresHumanGateLedger(DSN).pending() == ["deploy-prod"]
    # a service principal cannot clear it
    from hallpass.humangate import HumanGateError

    svc = Principal("bot", frozenset(), kind="service")
    with pytest.raises(HumanGateError):
        ledger.decide("deploy-prod", svc, approved=True)
    # a human clears it, and a fresh instance sees the cleared state
    human = Principal("alice", frozenset())
    gate = ledger.decide("deploy-prod", human, approved=True)
    assert gate.status == "approved"
    assert PostgresHumanGateLedger(DSN).cleared("deploy-prod")
    assert PostgresHumanGateLedger(DSN).pending() == []


# -- schema migration / concurrency-safe DDL -------------------------------


def test_pg_migrate_provisions_all_tables_and_records_version():
    from hallpass import SCHEMA_VERSION, migrate, schema_version

    _reset(
        "tasks",
        "a2a_policies",
        "credentials",
        "a2a_messages",
        "a2a_cursors",
        "a2a_presence",
        "audit",
        "revocations",
        "human_gates",
        "schema_version",
    )
    assert schema_version(DSN) == 0  # never migrated
    assert migrate(DSN) == SCHEMA_VERSION
    assert schema_version(DSN) == SCHEMA_VERSION
    # every table now exists (a construct against each backend is a no-op)
    import psycopg

    with psycopg.connect(DSN) as conn:
        for table in ("tasks", "credentials", "a2a_messages", "audit"):
            assert (
                conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
                is not None
            )
    # idempotent: a second run does not raise or duplicate the version row
    assert migrate(DSN) == SCHEMA_VERSION
    with psycopg.connect(DSN) as conn:
        n = conn.execute("SELECT count(*) FROM schema_version").fetchone()[0]
    assert n == 1


def test_pg_concurrent_construction_does_not_race_on_ddl():
    """N replicas booting at once all run CREATE TABLE/INDEX IF NOT EXISTS; the
    shared advisory lock must serialize them so none crashes with a catalog race
    ('tuple concurrently updated')."""
    from hallpass import PostgresA2AStore

    _reset("a2a_messages", "a2a_cursors", "a2a_presence")
    errors: list[Exception] = []

    def boot() -> None:
        try:
            PostgresA2AStore(DSN)  # constructor issues the DDL
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch a race
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []  # no DDL race under concurrent boot


# -- build(database_url=...) wiring ----------------------------------------


def test_pg_build_database_url_wires_durable_vault():
    """build(database_url=...) -- the path 'hallpass serve' takes from
    HALLPASS_DATABASE_URL -- puts the credential vault on Postgres: durable,
    and readiness reports the backend answering."""
    from cryptography.fernet import Fernet

    from hallpass import StaticJwks, build

    _reset("credentials")
    # database_url without redis_url warns (vault shared, ops stores per-process);
    # legitimate on a single node, which is what this test is.
    with pytest.warns(UserWarning, match="redis_url"):
        app = build(
            issuer="i",
            audience="a",
            jwks=StaticJwks({"keys": []}),
            # a shared Postgres vault requires a stable key (build refuses
            # without one -- a per-replica ephemeral key corrupts credentials)
            vault_key=Fernet.generate_key(),
            database_url=DSN,
        )
    assert app.vault_durable is True
    ready, checks = app.check_readiness()
    assert ready is True and checks["vault"] == "ok"
    app.close()
