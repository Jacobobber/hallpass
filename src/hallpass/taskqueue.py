"""A durable, lease-based task queue, so an agent crash doesn't lose or
double-run work.

The A2A orchestrator broadcasts and addresses tasks; that is coordination, not
durability. When a fleet of workers pulls from a shared backlog and any of them
can die mid-task, two properties matter and neither is free: work survives a
crash (it is on disk, not in a process), and each task runs once (two workers
never grab the same one). ``TaskQueue`` is those two properties on SQLite:

- ``enqueue`` writes a pending task to disk.
- ``claim`` hands one worker exactly one task under a write-locked transaction,
  so concurrent workers cannot claim the same task, and marks it leased.
- a lease expires: if the worker that claimed a task dies without completing it,
  the task becomes claimable again after ``lease_seconds`` (at-least-once), and
  ``complete`` is keyed by task id, so a re-run is idempotent to record.
- ``complete`` records the result; it survives a restart, and ``outstanding``
  lets a resuming orchestrator see what is still in flight.

This is a coordination/durability primitive; the auth boundary is still on the
tools a worker calls with its scoped token. Pass a file ``path`` for real
durability; ``:memory:`` is a single-process default.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .orchestrator import Result

__all__ = ["TaskQueue", "LeasedTask"]


@dataclass(frozen=True)
class LeasedTask:
    id: str
    do: str
    args: dict[str, str] = field(default_factory=dict)
    note: str = ""
    worker: str = ""  # who holds the lease


def _default_ids() -> str:
    return secrets.token_hex(6)


class TaskQueue:
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def enqueue(
        self, do: str, *, args: Mapping[str, str] | None = None, note: str = ""
    ) -> str:
        """Add a pending task; return its id."""
        task_id = self._ids()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, do, args, note, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (task_id, do, json.dumps(dict(args or {})), note, self._now()),
            )
        return task_id

    def claim(self, worker: str, *, lease_seconds: float = 60.0) -> LeasedTask | None:
        """Atomically hand ``worker`` one task: the oldest pending one, or one
        whose lease has expired (its previous worker went away). Returns None if
        there is nothing to do. The write-locked transaction is what stops two
        workers claiming the same task."""
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
        self,
        task_id: str,
        *,
        worker: str = "",
        ok: bool = True,
        fields: Mapping[str, str] | None = None,
        note: str = "",
    ) -> bool:
        """Record a task's result and mark it done. Idempotent by id and a
        no-op once a task is already done, so a re-run (after a lease expiry)
        cannot overwrite a recorded result. Returns True if this call is what
        marked it done."""
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
                    (
                        1 if ok else 0,
                        json.dumps(dict(fields or {})),
                        worker,
                        note,
                        task_id,
                    ),
                )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def result(self, task_id: str) -> Result | None:
        """The recorded result for a completed task, or None if not done."""
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
        """Ids of tasks not yet done (pending or leased) -- what a resuming
        orchestrator still needs results for."""
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
