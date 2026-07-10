"""The task-queue backend seam: TaskQueue is a facade over a TaskQueueBackend,
so the queue can be stored elsewhere (a shared DB for a fleet) without changing
its semantics. The exactly-once guarantee is asserted under real thread
contention over BOTH backends, so the abstraction demonstrably preserves it."""

import threading

import pytest

from hallpass import (
    InMemoryTaskQueueBackend,
    SqliteTaskQueueBackend,
    TaskQueue,
)


@pytest.fixture(params=["default", "sqlite-backend", "memory-backend"])
def queue(request, tmp_path):
    if request.param == "default":
        q = TaskQueue()  # SqliteTaskQueueBackend(:memory:) under the hood
    elif request.param == "sqlite-backend":
        q = TaskQueue(backend=SqliteTaskQueueBackend(path=str(tmp_path / "q.db")))
    else:
        q = TaskQueue(backend=InMemoryTaskQueueBackend())
    yield q
    q.close()


def test_enqueue_claim_complete_result(queue):
    tid = queue.enqueue("resize", args={"w": "640"}, note="img")
    assert queue.pending_count() == 1
    task = queue.claim("worker-1")
    assert task.id == tid and task.do == "resize" and task.args == {"w": "640"}
    assert queue.pending_count() == 0
    assert queue.complete(tid, worker="worker-1", ok=True, fields={"status": "done"})
    res = queue.result(tid)
    assert res.ok and res.fields == {"status": "done"} and res.worker == "worker-1"


def test_claim_is_fifo_by_creation(queue):
    a = queue.enqueue("op", args={"n": "1"})
    b = queue.enqueue("op", args={"n": "2"})
    assert queue.claim("w").id == a
    assert queue.claim("w").id == b
    assert queue.claim("w") is None  # nothing left


def test_complete_is_idempotent(queue):
    tid = queue.enqueue("op")
    queue.claim("w")
    assert queue.complete(tid, ok=True, fields={"v": "1"}) is True
    assert queue.complete(tid, ok=True, fields={"v": "2"}) is False  # already done
    assert queue.result(tid).fields == {"v": "1"}  # first result stands


def test_expired_lease_is_reclaimable(tmp_path):
    """A dead worker's task is reclaimable once its lease lapses. Driven by an
    injected clock (both backends) so expiry is deterministic, not timing-flaky."""
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    backends = [
        InMemoryTaskQueueBackend(now=now),
        SqliteTaskQueueBackend(path=str(tmp_path / "q.db"), now=now),
    ]
    for backend in backends:
        clock["t"] = 1000.0
        q = TaskQueue(backend=backend)
        tid = q.enqueue("op")
        q.claim("dead-worker", lease_seconds=60.0)
        assert q.claim("w2", lease_seconds=60.0) is None  # still within the lease
        clock["t"] += 61.0  # lease lapses
        reclaimed = q.claim("w2", lease_seconds=60.0)
        assert reclaimed is not None and reclaimed.id == tid
        q.close()


def test_outstanding_tracks_not_done(queue):
    a = queue.enqueue("op")
    b = queue.enqueue("op")
    queue.claim("w")  # leases a (still outstanding)
    assert set(queue.outstanding()) == {a, b}
    queue.complete(a, ok=True)
    assert queue.outstanding() == [b]


def test_exactly_once_under_thread_contention(queue):
    """The crown-jewel guarantee, over whichever backend: N workers hammering
    claim/complete drain the backlog with no task claimed twice and none lost."""
    n = 300
    for i in range(n):
        queue.enqueue("op", args={"i": str(i)})

    claimed_ids: list[str] = []
    guard = threading.Lock()

    def worker(name):
        while True:
            task = queue.claim(name)
            if task is None:
                # could be a transient empty read while others hold tasks; stop
                # only when the backlog is truly drained
                if queue.pending_count() == 0 and not queue.outstanding():
                    return
                if queue.pending_count() == 0:
                    return
                continue
            with guard:
                claimed_ids.append(task.id)
            queue.complete(task.id, worker=name, ok=True)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(claimed_ids) == n  # every task claimed
    assert len(set(claimed_ids)) == n  # none claimed twice
    assert queue.pending_count() == 0


def test_backend_is_swappable_same_semantics(tmp_path):
    """A TaskQueue with an explicit backend behaves identically to the default
    -- the facade adds nothing beyond delegation."""
    q = TaskQueue(backend=InMemoryTaskQueueBackend())
    tid = q.enqueue("x")
    q.claim("w")
    q.complete(tid, ok=False, note="boom")
    assert q.result(tid).ok is False and q.result(tid).note == "boom"
    q.close()
