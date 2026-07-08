"""Drive worker agents from an orchestrator agent, over hallpass.

This is the coordination layer the rest of hallpass composes into. An
orchestrator and its workers are each a ``Principal`` (typically a service
identity); they talk over one ``A2ABus`` channel, and the messages are FLEX:

    orchestrator --  task @worker #<id> do=<op> <args> | <note>  --> channel
    worker       --  result #<id> ok=true <fields> | <note>      --> channel
    orchestrator gathers results by task id.

Because it rides ``A2ABus``, the whole exchange is authorized (the channel's
scopes gate who may post and read), durable (a worker that dies mid-task sees
the task on reconnect), and audited (if the bus has an audit sink). Because it
is FLEX, the wire form is compact and parseable.

Delivery is at-least-once: a worker crash between handling and ack can redeliver
a task, so ``gather`` de-duplicates by task id (first result wins) and a handler
should be idempotent. Addressing is by convention on a shared channel (a worker
honours tasks sent to its subject); for hard isolation between workers, give
each its own channel with its own read scope.

Task ``args`` and result ``fields`` are FLEX field values, so they are
whitespace-free tokens (ids, enums, numbers); put freeform text in the ``note``.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from . import flex
from .a2a import A2ABus
from .identity import Principal

__all__ = ["Task", "Result", "Orchestrator", "Worker", "Handler", "Router"]


@dataclass(frozen=True)
class Task:
    id: str
    do: str  # which operation the worker should run
    args: dict[str, str] = field(default_factory=dict)
    note: str = ""
    sender: str = ""  # the orchestrator's subject


@dataclass(frozen=True)
class Result:
    task_id: str
    worker: str  # the subject that produced it
    ok: bool
    fields: dict[str, str] = field(default_factory=dict)
    note: str = ""


# A worker handler: given the task, return the result fields (token values).
# Raising marks the result failed; the exception type (never its message) is
# reported, so a handler error cannot leak detail onto the channel.
Handler = Callable[[Task], Mapping[str, str]]


def _default_ids() -> str:
    return secrets.token_hex(6)


class Orchestrator:
    """Dispatches tasks to workers and gathers their results over a channel."""

    def __init__(
        self,
        bus: A2ABus,
        principal: Principal,
        channel: str,
        *,
        ids: Callable[[], str] = _default_ids,
    ) -> None:
        self._bus = bus
        self._principal = principal
        self._channel = channel
        self._ids = ids

    def dispatch(
        self,
        worker: str,
        do: str,
        *,
        args: Mapping[str, str] | None = None,
        note: str = "",
    ) -> str:
        """Post a task addressed to ``worker`` and return its id. The id tags
        the message so the eventual result can be matched back to it."""
        task_id = self._ids()
        message = flex.Message(
            kind="task",
            to=(worker,),
            refs=(task_id,),
            fields={"do": do, **(args or {})},
            note=note,
        )
        self._bus.post(self._principal, self._channel, flex.encode(message))
        return task_id

    def gather(self, task_ids: Iterable[str], *, rounds: int = 1) -> dict[str, Result]:
        """Read the channel and return the results for ``task_ids`` seen so far,
        keyed by task id. De-duplicates by id (first result wins). ``rounds`` is
        how many read passes to make (each pass acks what it read)."""
        wanted = set(task_ids)
        found: dict[str, Result] = {}
        for _ in range(max(rounds, 1)):
            messages = self._bus.catch_up(self._principal, self._channel)
            if not messages:
                break
            for msg in messages:
                parsed = flex.parse(msg.body)
                if parsed.kind == "result" and parsed.refs:
                    tid = parsed.refs[0]
                    if tid in wanted and tid not in found:
                        found[tid] = Result(
                            task_id=tid,
                            worker=msg.sender,
                            ok=parsed.fields.get("ok") == "true",
                            fields={
                                k: v for k, v in parsed.fields.items() if k != "ok"
                            },
                            note=parsed.note,
                        )
            self._bus.ack(self._principal, self._channel, messages[-1].seq)
        return found


class Worker:
    """Runs registered handlers against tasks addressed to it on a channel."""

    def __init__(self, bus: A2ABus, principal: Principal, channel: str) -> None:
        self._bus = bus
        self._principal = principal
        self._channel = channel
        self._handlers: dict[str, Handler] = {}

    def handle(self, do: str) -> Callable[[Handler], Handler]:
        """Register a handler for a task operation. Returns the function
        unchanged so it stays directly callable and testable."""

        def register(fn: Handler) -> Handler:
            self._handlers[do] = fn
            return fn

        return register

    def run_once(self) -> int:
        """One pass: read unacked messages, run the handler for each task
        addressed to this worker, post a result, then ack. Returns how many
        tasks were handled. Loop this (or call it on a timer) to serve."""
        messages = self._bus.catch_up(self._principal, self._channel)
        if not messages:
            return 0
        handled = 0
        for msg in messages:
            parsed = flex.parse(msg.body)
            if parsed.kind != "task" or self._principal.subject not in parsed.to:
                continue
            if not parsed.refs:
                continue
            task = Task(
                id=parsed.refs[0],
                do=parsed.fields.get("do", ""),
                args={k: v for k, v in parsed.fields.items() if k != "do"},
                note=parsed.note,
                sender=msg.sender,
            )
            self._run(task)
            handled += 1
        self._bus.ack(self._principal, self._channel, messages[-1].seq)
        return handled

    def _run(self, task: Task) -> None:
        handler = self._handlers.get(task.do)
        if handler is None:
            self._reply(task, ok=False, fields={}, note="no handler")
            return
        try:
            result_fields = dict(handler(task))
            self._reply(task, ok=True, fields=result_fields, note="")
        except Exception as exc:  # noqa: BLE001 - report failure, never the detail
            self._reply(task, ok=False, fields={}, note=type(exc).__name__)

    def _reply(
        self, task: Task, *, ok: bool, fields: dict[str, str], note: str
    ) -> None:
        message = flex.Message(
            kind="result",
            refs=(task.id,),
            fields={"ok": "true" if ok else "false", **fields},
            note=note,
        )
        self._bus.post(self._principal, self._channel, flex.encode(message))


class Router:
    """Route a task to a worker by capability, where capability is the auth
    scope set. Each worker registers its harness (the scopes its token carries);
    a task declares the scopes it needs; ``route`` returns a worker whose harness
    covers them, round-robin across the eligible ones. Auth-native: the same
    scopes that gate tool calls decide who is *capable* of a task, so work never
    lands on an agent that could not perform it anyway (and if none is capable,
    that is visible, not a silent misroute). Pair it with ``dispatch`` or a
    ``TaskQueue``: route first, then hand the task to the chosen worker."""

    def __init__(self) -> None:
        self._workers: dict[str, frozenset[str]] = {}
        self._rr = 0
        self._lock = threading.Lock()

    def register(self, worker: str, scopes: Iterable[str]) -> None:
        """Declare a worker and the scopes its harness grants."""
        with self._lock:
            self._workers[worker] = frozenset(scopes)

    def candidates(self, required: Iterable[str]) -> list[str]:
        """Every registered worker whose harness covers ``required``, sorted."""
        need = frozenset(required)
        with self._lock:
            return sorted(w for w, s in self._workers.items() if need <= s)

    def route(self, required: Iterable[str]) -> str | None:
        """A capable worker for a task needing ``required`` scopes, or None if
        none is capable. Round-robins across the eligible workers so repeated
        routes spread the load."""
        eligible = self.candidates(required)
        if not eligible:
            return None
        with self._lock:
            pick = eligible[self._rr % len(eligible)]
            self._rr += 1
        return pick
