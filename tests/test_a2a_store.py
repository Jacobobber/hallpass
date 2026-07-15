"""The A2A message log / cursors / presence live behind an ``A2AStore`` so the
bus can run on SQLite by default or a shared database for a fleet. These tests
pin the two guarantees every backend must keep -- a monotonic, gap-free
per-channel sequence and a forward-only cursor -- against both stock backends,
plus that a bus works unchanged when handed a store instead of a path."""

import threading

import pytest

from hallpass import (
    A2ABus,
    ChannelPolicy,
    InMemoryA2AStore,
    Principal,
    SqliteA2AStore,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryA2AStore()
    else:
        s = SqliteA2AStore(path=str(tmp_path / "a2a.db"))
        yield s
        s.close()


def test_append_assigns_monotonic_seq(store):
    assert store.append("build", "orch", "one", 1.0) == 1
    assert store.append("build", "orch", "two", 2.0) == 2
    assert store.append("build", "orch", "three", 3.0) == 3
    # sequence is per channel, so a different channel starts at 1 again
    assert store.append("ops", "orch", "a", 4.0) == 1
    assert store.head("build") == 3
    assert store.head("ops") == 1
    assert store.head("never-used") == 0


def test_read_after_paginates_in_order(store):
    for i in range(5):
        store.append("c", "s", f"m{i}", float(i))
    first = store.read_after("c", 0, 2)
    assert [m[0] for m in first] == [1, 2]  # seqs
    assert [m[2] for m in first] == ["m0", "m1"]  # bodies
    rest = store.read_after("c", first[-1][0], 100)
    assert [m[0] for m in rest] == [3, 4, 5]
    assert store.read_after("c", 5, 100) == []


def test_cursor_is_forward_only(store):
    assert store.cursor("worker", "c") == 0  # never acked
    assert store.advance_cursor("worker", "c", 3) == 3
    assert store.advance_cursor("worker", "c", 5) == 5
    # a stale ack cannot regress the cursor
    assert store.advance_cursor("worker", "c", 2) == 5
    assert store.cursor("worker", "c") == 5
    # cursors are per (subject, channel)
    assert store.cursor("other", "c") == 0


def test_presence_ages_off_by_timestamp(store):
    store.touch_presence("c", "alice", 100.0)
    store.touch_presence("c", "bob", 100.0)
    store.touch_presence("other", "carol", 100.0)
    assert store.roster("c", 50.0) == ["alice", "bob"]  # both recent enough, sorted
    assert store.roster("c", 150.0) == []  # both too old for this cutoff
    # a refresh moves a subject back onto a stricter roster
    store.touch_presence("c", "alice", 200.0)
    assert store.roster("c", 150.0) == ["alice"]


def test_append_is_exactly_once_across_threads(store):
    """The seq must be unique under concurrency -- the lock (SQLite:
    BEGIN IMMEDIATE) is what makes concurrent posters never collide."""
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
        t.join(timeout=30)
    assert len(seqs) == n
    assert len(set(seqs)) == n  # no two posts got the same seq
    assert sorted(seqs) == list(range(1, n + 1))  # gap-free 1..n
    assert store.head("hot") == n


def test_bus_accepts_an_injected_store(tmp_path):
    """A bus handed a store behaves exactly like the default path-based one;
    the store is where durability lives, the bus is where authz lives."""
    store = SqliteA2AStore(path=str(tmp_path / "shared.db"))
    bus = A2ABus(store=store)
    bus.declare_channel(
        "build",
        ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"})),
    )
    writer = Principal("orch", frozenset({"w"}))
    reader = Principal("worker", frozenset({"r"}))
    bus.post(writer, "build", "task")
    got = bus.catch_up(reader, "build")
    assert [m.body for m in got] == ["task"]
    bus.ack(reader, "build", got[-1].seq)
    assert bus.catch_up(reader, "build") == []  # acked, nothing redelivered
    bus.close()


def test_two_buses_share_one_store(tmp_path):
    """Durability is in the store: a second bus on the same store sees the
    first bus's messages (a stand-in for two replicas on shared storage)."""
    store_a = SqliteA2AStore(path=str(tmp_path / "log.db"))
    store_b = SqliteA2AStore(path=str(tmp_path / "log.db"))
    policy = ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"}))
    bus_a = A2ABus(store=store_a)
    bus_b = A2ABus(store=store_b)
    for bus in (bus_a, bus_b):
        bus.declare_channel("build", policy)  # policies are per-bus here
    bus_a.post(Principal("orch", frozenset({"w"})), "build", "hello")
    got = bus_b.catch_up(Principal("worker", frozenset({"r"})), "build")
    assert [m.body for m in got] == ["hello"]
    bus_a.close()
    bus_b.close()
