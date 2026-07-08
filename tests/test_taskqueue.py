"""A durable, lease-based work queue: work survives a crash and each task runs
once. The properties that matter and are easy to get wrong: two workers never
claim the same task, a claimed-but-abandoned task becomes claimable again after
its lease expires (so a dead worker doesn't strand it), completing is idempotent
by id (a re-run can't overwrite a recorded result), and the whole thing survives
being reopened (a restart)."""

from hallpass import LeasedTask, TaskQueue


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_enqueue_claim_complete_result():
    q = TaskQueue()
    tid = q.enqueue("resize", args={"w": "1024"}, note="batch 7")
    leased = q.claim("w1")
    assert isinstance(leased, LeasedTask)
    assert leased.id == tid and leased.do == "resize"
    assert leased.args == {"w": "1024"} and leased.note == "batch 7"
    assert leased.worker == "w1"
    assert q.complete(tid, worker="w1", ok=True, fields={"status": "done"}) is True
    res = q.result(tid)
    assert res.ok is True and res.fields == {"status": "done"} and res.worker == "w1"
    q.close()


def test_claim_gives_each_task_to_one_worker():
    q = TaskQueue()
    q.enqueue("a")
    q.enqueue("b")
    first = q.claim("w1")
    second = q.claim("w2")
    third = q.claim("w3")
    assert {first.do, second.do} == {"a", "b"}  # two distinct tasks
    assert first.id != second.id
    assert third is None  # queue drained; no double-hand-out
    q.close()


def test_empty_queue_claim_returns_none():
    q = TaskQueue()
    assert q.claim("w1") is None
    q.close()


def test_expired_lease_becomes_claimable_again():
    clock = Clock()
    q = TaskQueue(now=clock)
    tid = q.enqueue("job")
    a = q.claim("w1", lease_seconds=60.0)
    assert a.id == tid
    assert q.claim("w2", lease_seconds=60.0) is None  # still leased by w1
    clock.t += 61.0  # w1's lease expires (it died without completing)
    b = q.claim("w2", lease_seconds=60.0)
    assert b is not None and b.id == tid and b.worker == "w2"  # reclaimed
    q.close()


def test_complete_is_idempotent_and_ignores_stale_rerun():
    clock = Clock()
    q = TaskQueue(now=clock)
    tid = q.enqueue("job")
    q.claim("w1", lease_seconds=10.0)
    assert q.complete(tid, worker="w1", ok=True, fields={"n": "1"}) is True
    # a stale re-run (e.g. a slow duplicate) cannot overwrite the recorded result
    assert q.complete(tid, worker="w2", ok=True, fields={"n": "2"}) is False
    assert q.result(tid).fields == {"n": "1"}
    q.close()


def test_outstanding_and_pending_counts():
    q = TaskQueue()
    a = q.enqueue("a")
    b = q.enqueue("b")
    assert q.pending_count() == 2
    q.claim("w1")  # one leased -> still outstanding, not pending
    assert q.pending_count() == 1
    assert set(q.outstanding()) == {a, b}
    q.complete(a, ok=True)
    q.complete(b, ok=True)
    assert q.outstanding() == []
    q.close()


def test_survives_across_instances(tmp_path):
    path = str(tmp_path / "q.sqlite3")
    q1 = TaskQueue(path=path)
    tid = q1.enqueue("job", args={"x": "1"})
    q1.claim("w1")
    q1.close()
    # a fresh process resumes: the task is still outstanding, still claimable
    q2 = TaskQueue(path=path)
    assert q2.outstanding() == [tid]
    q2.complete(tid, worker="w1", ok=True, fields={"done": "yes"})
    assert q2.result(tid).fields == {"done": "yes"}
    q2.close()
