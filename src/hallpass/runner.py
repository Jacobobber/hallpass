"""Reference agent runners: the reusable worker loops a served agent runs.

hallpass provisions identity, scope, channels, and a durable queue, but it runs
no model loop of its own -- that is deliberate (see "What this is not"). What a
spawned agent still needs is the *loop* around its work: claim or receive a
task, run it, report, heartbeat so it stays on the roster, and stop cleanly.
That loop is the same whichever model (or no model) sits inside a handler, so it
ships here once instead of being rewritten per agent.

Two forms, one for each delivery primitive:

- ``run_worker`` drives an orchestrator :class:`~hallpass.Worker` over an A2A
  channel (``run_once`` until told to stop).
- ``serve_queue`` claims from a durable :class:`~hallpass.TaskQueue`, dispatches
  to a handler by operation, and completes the task (idempotent, lease-safe).

Both take an optional ``stop`` predicate and ``max_idle_rounds`` so a caller can
run-and-drain or serve-forever, an injectable ``sleep`` and ``heartbeat`` (wire
``bus.announce`` into it to keep a live-roster seat), and both report failure
the way the rest of hallpass does: a handler that raises completes the task
``ok=False`` with only the exception *type* as the note, never its message.
The auth boundary is unchanged -- a handler acts with the agent's scoped token.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from .orchestrator import Worker
from .taskqueue import LeasedTask, TaskQueue

__all__ = ["run_worker", "serve_queue", "QueueHandler"]

# A queue handler: given the claimed task, return the result fields (token
# values). Raising marks the task failed; only the exception type is recorded.
QueueHandler = Callable[[LeasedTask], Mapping[str, str]]


def _terminates(stop: Callable[[], bool] | None, max_idle_rounds: int | None) -> bool:
    # With neither a stop predicate nor an idle bound, the loop would spin
    # forever; instead it means "drain what's here and return" -- one idle round
    # ends it. This keeps the no-argument call safe and useful.
    return stop is None and max_idle_rounds is None


def run_worker(
    worker: Worker,
    *,
    stop: Callable[[], bool] | None = None,
    poll: float = 0.05,
    max_idle_rounds: int | None = None,
    heartbeat: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Drive a :class:`Worker`'s ``run_once`` loop, returning the total tasks
    handled. Runs until ``stop()`` is true or ``max_idle_rounds`` consecutive
    empty passes (whichever comes first); with neither set it drains the channel
    once and returns. Non-empty passes drain back-to-back without sleeping;
    ``sleep(poll)`` runs only between idle passes. ``heartbeat`` is called each
    pass -- point it at ``bus.announce`` to hold a live-roster seat."""
    handled = 0
    idle = 0
    while True:
        if stop is not None and stop():
            break
        if heartbeat is not None:
            heartbeat()
        n = worker.run_once()
        handled += n
        if n:
            idle = 0
            continue
        idle += 1
        if max_idle_rounds is not None and idle >= max_idle_rounds:
            break
        if _terminates(stop, max_idle_rounds):
            break
        sleep(poll)
    return handled


def serve_queue(
    queue: TaskQueue,
    worker: str,
    handlers: Mapping[str, QueueHandler],
    *,
    stop: Callable[[], bool] | None = None,
    lease_seconds: float = 60.0,
    poll: float = 0.05,
    max_idle_rounds: int | None = None,
    heartbeat: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Claim, run, and complete tasks from a :class:`TaskQueue`, returning the
    total completed. Each pass claims one task under the queue's lease and
    dispatches it to ``handlers[task.do]``: the handler's returned mapping
    becomes the result fields (``ok=True``); an unknown operation completes
    ``ok=False`` note ``"no handler"``; a raising handler completes ``ok=False``
    with the exception *type* as the note. Termination and ``heartbeat`` behave
    as in :func:`run_worker`. ``complete`` is idempotent, so an expired-lease
    re-run cannot overwrite a recorded result."""
    completed = 0
    idle = 0
    while True:
        if stop is not None and stop():
            break
        if heartbeat is not None:
            heartbeat()
        task = queue.claim(worker, lease_seconds=lease_seconds)
        if task is None:
            idle += 1
            if max_idle_rounds is not None and idle >= max_idle_rounds:
                break
            if _terminates(stop, max_idle_rounds):
                break
            sleep(poll)
            continue
        idle = 0
        handler = handlers.get(task.do)
        if handler is None:
            queue.complete(task.id, worker=worker, ok=False, note="no handler")
        else:
            try:
                fields = dict(handler(task))
                queue.complete(task.id, worker=worker, ok=True, fields=fields)
            except Exception as exc:  # noqa: BLE001 - report failure, not detail
                queue.complete(
                    task.id, worker=worker, ok=False, note=type(exc).__name__
                )
        completed += 1
    return completed
