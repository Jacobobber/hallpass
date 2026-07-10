"""A durable, lease-based task queue, so an agent crash doesn't lose or
double-run work.

The A2A orchestrator broadcasts and addresses tasks; that is coordination, not
durability. When a fleet of workers pulls from a shared backlog and any of them
can die mid-task, two properties matter and neither is free: work survives a
crash (it is on disk, not in a process), and each task runs once (two workers
never grab the same one). ``TaskQueue`` is those two properties:

- ``enqueue`` writes a pending task.
- ``claim`` hands one worker exactly one task under a write lock, so concurrent
  workers cannot claim the same task, and marks it leased.
- a lease expires: if the worker that claimed a task dies without completing it,
  the task becomes claimable again after ``lease_seconds`` (at-least-once), and
  ``complete`` is keyed by task id, so a re-run is idempotent to record.
- ``complete`` records the result; it survives a restart, and ``outstanding``
  lets a resuming orchestrator see what is still in flight.

*Where* the queue is stored is a ``TaskQueueBackend``: SQLite by default
(``SqliteTaskQueueBackend``), in-memory for tests (``InMemoryTaskQueueBackend``),
or a shared database for a multi-replica fleet. A Postgres backend implements
``claim`` with ``SELECT … FOR UPDATE SKIP LOCKED`` — the exactly-once guarantee
each backend keeps its own way, behind one interface. This is a
coordination/durability primitive; the auth boundary is still on the tools a
worker calls with its scoped token.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .orchestrator import Result

__all__ = [
    "TaskQueue",
    "LeasedTask",
    "TaskQueueBackend",
    "InMemoryTaskQueueBackend",
    "SqliteTaskQueueBackend",
]


@dataclass(frozen=True)
class LeasedTask:
    id: str
    do: str
    args: dict[str, str] = field(default_factory=dict)
    note: str = ""
    worker: str = ""  # who holds the lease


def _default_ids() -> str:
    return secrets.token_hex(6)


class TaskQueueBackend(Protocol):
    """Storage for the queue. Each backend keeps the two guarantees its own way
    (SQLite: a write-locked ``BEGIN IMMEDIATE``; Postgres: ``FOR UPDATE SKIP
    LOCKED``); the semantics are identical behind the interface."""

    def enqueue(self, do: str, args: Mapping[str, str], note: str) -> str: ...
    def claim(self, worker: str, lease_seconds: float) -> LeasedTask | None: ...
    def complete(
        self, task_id: str, worker: str, ok: bool, fields: Mapping[str, str], note: str
    ) -> bool: ...
    def result(self, task_id: str) -> Result | None: ...
    def outstanding(self) -> list[str]: ...
    def pending_count(self) -> int: ...
    def close(self) -> None: ...


@dataclass
class _Task:
    id: str
    do: str
    args: dict[str, str]
    note: str
    status: str
    created_at: float
    leased_at: float = 0.0
    ok: bool = False
    result: dict[str, str] = field(default_factory=dict)
    worker: str = ""


class InMemoryTaskQueueBackend:
    """Process-local queue storage; thread-safe (the lock is what makes ``claim``
    exactly-once across threads), not durable."""

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        ids: Callable[[], str] = _default_ids,
    ) -> None:
        self._now = now or time.time
        self._ids = ids
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.Lock()

    def enqueue(self, do: str, args: Mapping[str, str], note: str) -> str:
        task_id = self._ids()
        with self._lock:
            self._tasks[task_id] = _Task(
                id=task_id,
                do=do,
                args=dict(args),
                note=note,
                status="pending",
                created_at=self._now(),
            )
        return task_id

    def claim(self, worker: str, lease_seconds: float) -> LeasedTask | None:
        cutoff = self._now() - lease_seconds
        with self._lock:
            claimable = [
                t
                for t in self._tasks.values()
                if t.status == "pending"
                or (t.status == "leased" and t.leased_at < cutoff)
            ]
            if not claimable:
                return None
            task = min(claimable, key=lambda t: t.created_at)
            task.status = "leased"
            task.worker = worker
            task.leased_at = self._now()
            return LeasedTask(
                id=task.id,
                do=task.do,
                args=dict(task.args),
                note=task.note,
                worker=worker,
            )

    def complete(
        self, task_id: str, worker: str, ok: bool, fields: Mapping[str, str], note: str
    ) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status == "done":
                return False
            task.status = "done"
            task.ok = ok
            task.result = dict(fields)
            task.worker = worker
            task.note = note
            return True

    def result(self, task_id: str) -> Result | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "done":
                return None
            return Result(
                task_id=task_id,
                worker=task.worker,
                ok=task.ok,
                fields=dict(task.result),
                note=task.note,
            )

    def outstanding(self) -> list[str]:
        with self._lock:
            pending = [t for t in self._tasks.values() if t.status != "done"]
        return [t.id for t in sorted(pending, key=lambda t: t.created_at)]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "pending")

    def close(self) -> None:
        pass


class SqliteTaskQueueBackend:
    """Durable queue storage on SQLite. ``claim`` runs under ``BEGIN IMMEDIATE``
    (the write lock) so two workers cannot claim the same task; pass a file
    ``path`` for durability, ``:memory:`` for a single-process default."""

    def __init__(
        self,
        *,
        path: str = ":memory:",
        now: Callable[[], float] | None = None,
        ids: Callable[[], str] = _default_ids,
    ) -> None:
        self._now = now or time.time
        self._ids = ids
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                " id TEXT PRIMARY KEY, do TEXT NOT NULL, args TEXT NOT NULL,"
                " note TEXT NOT NULL, status TEXT NOT NULL,"
                " leased_by TEXT, leased_at REAL,"
                " ok INTEGER, result TEXT, worker TEXT, created_at REAL NOT NULL)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_created"
                " ON tasks(status, created_at)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def enqueue(self, do: str, args: Mapping[str, str], note: str) -> str:
        task_id = self._ids()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, do, args, note, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (task_id, do, json.dumps(dict(args)), note, self._now()),
            )
        return task_id

    def claim(self, worker: str, lease_seconds: float) -> LeasedTask | None:
        cutoff = self._now() - lease_seconds
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id, do, args, note FROM tasks"
                    " WHERE status = 'pending'"
                    "    OR (status = 'leased' AND leased_at < ?)"
                    " ORDER BY created_at LIMIT 1",
                    (cutoff,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE tasks SET status='leased', leased_by=?, leased_at=?"
                    " WHERE id=?",
                    (worker, self._now(), row[0]),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return LeasedTask(
            id=row[0], do=row[1], args=json.loads(row[2]), note=row[3], worker=worker
        )

    def complete(
        self, task_id: str, worker: str, ok: bool, fields: Mapping[str, str], note: str
    ) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None or row[0] == "done":
                    self._conn.execute("COMMIT")
                    return False
                self._conn.execute(
                    "UPDATE tasks SET status='done', ok=?, result=?, worker=?,"
                    " note=? WHERE id=?",
                    (1 if ok else 0, json.dumps(dict(fields)), worker, note, task_id),
                )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def result(self, task_id: str) -> Result | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ok, result, worker, note FROM tasks"
                " WHERE id=? AND status='done'",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return Result(
            task_id=task_id,
            worker=row[2] or "",
            ok=bool(row[0]),
            fields=json.loads(row[1]) if row[1] else {},
            note=row[3] or "",
        )

    def outstanding(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE status != 'done' ORDER BY created_at"
            ).fetchall()
        return [r[0] for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='pending'"
            ).fetchone()
        return int(row[0])


class TaskQueue:
    """A durable, lease-based task queue. Thin facade over a ``TaskQueueBackend``
    (SQLite by default); pass ``backend=`` to store the queue elsewhere (a shared
    database for a multi-replica fleet). The public API is unchanged from before
    the backend seam existed."""

    def __init__(
        self,
        *,
        path: str = ":memory:",
        now: Callable[[], float] | None = None,
        ids: Callable[[], str] = _default_ids,
        backend: TaskQueueBackend | None = None,
    ) -> None:
        self._backend: TaskQueueBackend = backend or SqliteTaskQueueBackend(
            path=path, now=now, ids=ids
        )

    def close(self) -> None:
        self._backend.close()

    def enqueue(
        self, do: str, *, args: Mapping[str, str] | None = None, note: str = ""
    ) -> str:
        """Add a pending task; return its id."""
        return self._backend.enqueue(do, args or {}, note)

    def claim(self, worker: str, *, lease_seconds: float = 60.0) -> LeasedTask | None:
        """Atomically hand ``worker`` one task: the oldest pending one, or one
        whose lease has expired. Returns None if there is nothing to do."""
        return self._backend.claim(worker, lease_seconds)

    def complete(
        self,
        task_id: str,
        *,
        worker: str = "",
        ok: bool = True,
        fields: Mapping[str, str] | None = None,
        note: str = "",
    ) -> bool:
        """Record a task's result and mark it done. Idempotent by id; a no-op
        once done, so a re-run cannot overwrite a recorded result. Returns True
        if this call is what marked it done."""
        return self._backend.complete(task_id, worker, ok, fields or {}, note)

    def result(self, task_id: str) -> Result | None:
        """The recorded result for a completed task, or None if not done."""
        return self._backend.result(task_id)

    def outstanding(self) -> list[str]:
        """Ids of tasks not yet done -- what a resuming orchestrator still needs
        results for."""
        return self._backend.outstanding()

    def pending_count(self) -> int:
        return self._backend.pending_count()
