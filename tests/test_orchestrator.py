"""The orchestrator drives workers over an A2A channel in FLEX. What matters:
a dispatched task reaches the addressed worker and its result comes back matched
by id; a worker ignores tasks not addressed to it; a handler failure becomes a
failed result that never leaks the exception detail; results de-duplicate by id;
and the channel's scopes still gate who may dispatch and gather (the harness does
not bypass hallpass's auth)."""

import itertools

import pytest

from hallpass import (
    A2ABus,
    ChannelDenied,
    ChannelPolicy,
    Orchestrator,
    Principal,
    Worker,
    flex,
)


def _ids():
    counter = itertools.count(1)
    return lambda: f"t{next(counter)}"


def _setup(policy=None):
    bus = A2ABus()
    bus.declare_channel("work", policy or ChannelPolicy())
    orch = Orchestrator(bus, Principal("orch", frozenset()), "work", ids=_ids())
    return bus, orch


def test_dispatch_run_gather_end_to_end():
    bus, orch = _setup()
    worker = Worker(bus, Principal("w1", frozenset()), "work")

    seen = {}

    @worker.handle("resize")
    def resize(task):
        seen["task"] = task
        return {"status": "done", "px": task.args["size"]}

    tid = orch.dispatch("w1", "resize", args={"size": "1024"}, note="batch 7")
    assert worker.run_once() == 1
    results = orch.gather([tid])

    assert seen["task"].do == "resize"
    assert seen["task"].args == {"size": "1024"}
    assert seen["task"].note == "batch 7"
    assert results[tid].ok is True
    assert results[tid].worker == "w1"
    assert results[tid].fields == {"status": "done", "px": "1024"}


def test_worker_ignores_tasks_not_addressed_to_it():
    bus, orch = _setup()
    w2 = Worker(bus, Principal("w2", frozenset()), "work")
    ran = []

    @w2.handle("job")
    def job(task):
        ran.append(task.id)
        return {}

    orch.dispatch("w1", "job")  # addressed to w1, not w2
    assert w2.run_once() == 0  # w2 sees it on the channel but it is not for it
    assert ran == []


def test_failed_handler_reports_failure_without_leaking():
    bus, orch = _setup()
    worker = Worker(bus, Principal("w1", frozenset()), "work")

    @worker.handle("boom")
    def boom(task):
        raise RuntimeError("secret internal detail")

    tid = orch.dispatch("w1", "boom")
    worker.run_once()
    result = orch.gather([tid])[tid]
    assert result.ok is False
    assert result.note == "RuntimeError"  # type only
    assert "secret" not in result.note


def test_unknown_operation_is_a_failed_result():
    bus, orch = _setup()
    worker = Worker(bus, Principal("w1", frozenset()), "work")  # no handlers
    tid = orch.dispatch("w1", "nope")
    worker.run_once()
    result = orch.gather([tid])[tid]
    assert result.ok is False and result.note == "no handler"


def test_gather_dedupes_by_task_id_first_wins():
    bus, orch = _setup()
    w = Principal("w1", frozenset())
    # two results for the same task id (as a redelivery would produce)
    bus.post(
        w,
        "work",
        flex.encode(
            flex.Message("result", refs=("t1",), fields={"ok": "true", "n": "1"})
        ),
    )
    bus.post(
        w,
        "work",
        flex.encode(
            flex.Message("result", refs=("t1",), fields={"ok": "true", "n": "2"})
        ),
    )
    results = orch.gather(["t1"])
    assert results["t1"].fields["n"] == "1"  # first wins


def test_channel_scopes_still_gate_the_orchestrator():
    policy = ChannelPolicy(
        post_scopes=frozenset({"work:post"}), read_scopes=frozenset({"work:read"})
    )
    bus, _ = _setup(policy)
    # an orchestrator without the post scope cannot dispatch
    unscoped = Orchestrator(bus, Principal("x", frozenset()), "work", ids=_ids())
    with pytest.raises(ChannelDenied):
        unscoped.dispatch("w1", "job")
    # a scoped one can
    scoped = Orchestrator(
        bus, Principal("orch", frozenset({"work:post"})), "work", ids=_ids()
    )
    assert scoped.dispatch("w1", "job")  # returns a task id, no raise


def test_multiple_tasks_gather_together():
    bus, orch = _setup()
    worker = Worker(bus, Principal("w1", frozenset()), "work")

    @worker.handle("echo")
    def echo(task):
        return {"got": task.args["x"]}

    ids = [orch.dispatch("w1", "echo", args={"x": str(i)}) for i in range(3)]
    worker.run_once()
    results = orch.gather(ids)
    assert {results[i].fields["got"] for i in ids} == {"0", "1", "2"}
