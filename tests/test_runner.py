"""Reference agent runners: the reusable worker loops around a Worker (A2A) and
a TaskQueue (durable). Each test names the property it pins -- drain semantics,
clean stop, heartbeat, and failure reported as type-only. Sleep is injected so
the loops are tested without wall-clock waits."""

import pytest

from hallpass import (
    A2ABus,
    ChannelPolicy,
    Orchestrator,
    Principal,
    TaskQueue,
    Worker,
    run_worker,
    serve_queue,
)


def principal(subject, *scopes):
    return Principal(subject=subject, scopes=frozenset(scopes))


# -- run_worker (A2A) ------------------------------------------------------


@pytest.fixture()
def channel():
    bus = A2ABus()
    bus.declare_channel("work", ChannelPolicy())
    yield bus
    bus.close()


def _worker_with_resize(bus):
    worker = Worker(bus, principal("resizer"), "work")

    @worker.handle("resize")
    def resize(task):
        return {"width": task.args["width"]}

    return worker


def test_run_worker_drains_and_returns_with_no_termination(channel):
    """With neither stop nor max_idle_rounds, run_worker drains what is on the
    channel once and returns -- it does not spin forever."""
    orch = Orchestrator(channel, principal("orch"), "work")
    orch.dispatch("resizer", "resize", args={"width": "640"})
    orch.dispatch("resizer", "resize", args={"width": "1024"})
    worker = _worker_with_resize(channel)
    handled = run_worker(worker, sleep=_no_sleep())
    assert handled == 2


def test_run_worker_stops_on_predicate(channel):
    """A stop predicate ends the loop; work dispatched before stop is still
    handled."""
    orch = Orchestrator(channel, principal("orch"), "work")
    orch.dispatch("resizer", "resize", args={"width": "640"})
    worker = _worker_with_resize(channel)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2  # allow a couple of passes

    handled = run_worker(worker, stop=stop, max_idle_rounds=None, sleep=_no_sleep())
    assert handled == 1


def test_run_worker_max_idle_rounds_ends_an_idle_loop(channel):
    """An idle channel ends after max_idle_rounds empty passes rather than
    running forever."""
    worker = _worker_with_resize(channel)
    slept = []
    handled = run_worker(
        worker, max_idle_rounds=3, poll=0.01, sleep=lambda s: slept.append(s)
    )
    assert handled == 0
    assert len(slept) == 2  # sleeps between the 3 idle passes, not after the last


def test_run_worker_heartbeat_is_called_each_pass(channel):
    beats = {"n": 0}
    worker = _worker_with_resize(channel)
    run_worker(
        worker,
        max_idle_rounds=2,
        heartbeat=lambda: beats.__setitem__("n", beats["n"] + 1),
        sleep=_no_sleep(),
    )
    assert beats["n"] == 2


def test_run_worker_heartbeat_can_hold_a_roster_seat():
    """Wiring announce into the heartbeat keeps the worker on the live roster
    -- the runner ties presence and serving together."""
    bus = A2ABus()
    bus.declare_channel(
        "work",
        ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"w"})),
    )
    wp = principal("resizer", "w")
    worker = Worker(bus, wp, "work")
    run_worker(
        worker,
        max_idle_rounds=1,
        heartbeat=lambda: bus.announce(wp, "work"),
        sleep=_no_sleep(),
    )
    assert bus.roster(wp, "work") == ["resizer"]
    bus.close()


# -- serve_queue (durable) -------------------------------------------------


def test_serve_queue_completes_tasks():
    q = TaskQueue()
    a = q.enqueue("resize", args={"width": "640"})
    b = q.enqueue("resize", args={"width": "1024"})
    handlers = {"resize": lambda t: {"width": t.args["width"]}}
    done = serve_queue(q, "w1", handlers, sleep=_no_sleep())
    assert done == 2
    assert q.result(a).ok and q.result(a).fields == {"width": "640"}
    assert q.result(b).fields == {"width": "1024"}
    q.close()


def test_serve_queue_unknown_op_fails_cleanly():
    q = TaskQueue()
    tid = q.enqueue("frobnicate")
    done = serve_queue(q, "w1", {}, sleep=_no_sleep())
    assert done == 1
    res = q.result(tid)
    assert not res.ok
    assert res.note == "no handler"
    q.close()


def test_serve_queue_handler_error_reports_type_only():
    """A raising handler must not leak its message onto the record -- only the
    exception type, mirroring the orchestrator Worker."""
    q = TaskQueue()
    tid = q.enqueue("boom")

    def boom(task):
        raise ValueError("secret detail that must not surface")

    done = serve_queue(q, "w1", {"boom": boom}, sleep=_no_sleep())
    assert done == 1
    res = q.result(tid)
    assert not res.ok
    assert res.note == "ValueError"
    assert "secret" not in res.note
    q.close()


def test_serve_queue_stops_on_predicate_and_leaves_backlog():
    q = TaskQueue()
    q.enqueue("resize", args={"width": "1"})
    q.enqueue("resize", args={"width": "2"})
    handlers = {"resize": lambda t: {"w": t.args["width"]}}
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > 1  # one claim/complete, then stop

    done = serve_queue(q, "w1", handlers, stop=stop, sleep=_no_sleep())
    assert done == 1
    assert q.pending_count() == 1  # the second task is left for another worker
    q.close()


def test_serve_queue_drains_then_returns_with_no_termination():
    q = TaskQueue()
    q.enqueue("noop")
    done = serve_queue(q, "w1", {"noop": lambda t: {}}, sleep=_no_sleep())
    assert done == 1
    assert q.pending_count() == 0


def _no_sleep():
    return lambda _s: None
