"""Postgres backends for the coordination and credential stores.

These implement the same protocols as the SQLite defaults, so an operator points
a multi-replica deployment at Postgres by swapping the backend at construction —
no other change. The one that matters most is the queue: ``claim`` uses
``SELECT … FOR UPDATE SKIP LOCKED``, so many workers on many replicas pull from
one backlog with each task going to exactly one worker, without a global lock.

Postgres is an optional dependency (the ``postgres`` extra, ``psycopg``); the
import is deferred, so a core install is unaffected. Each backend opens a
short-lived connection per operation (simple and thread-safe; a connection pool
is the production optimization), so concurrent claims genuinely run on separate
connections — which is what makes SKIP LOCKED do its job.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from .a2a import ChannelPolicy
from .orchestrator import Result
from .taskqueue import LeasedTask

__all__ = [
    "PostgresTaskQueueBackend",
    "PostgresChannelPolicyStore",
    "PostgresVaultBackend",
]


def _default_ids() -> str:
    return secrets.token_hex(6)


def _connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn)


class PostgresTaskQueueBackend:
    """Durable task-queue storage on Postgres. ``claim`` is a single
    ``UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …``,
    the standard exactly-once work-queue pattern: concurrent claimants skip each
    other's locked rows instead of blocking, and no two get the same task."""

    def __init__(
        self,
        dsn: str,
        *,
        now: Callable[[], float] | None = None,
        ids: Callable[[], str] = _default_ids,
    ) -> None:
        self._dsn = dsn
        self._now = now or time.time
        self._ids = ids
        with _connect(dsn) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                # "do" is a reserved word in Postgres (unlike SQLite) -> quote it
                ' id TEXT PRIMARY KEY, "do" TEXT NOT NULL, args TEXT NOT NULL,'
                " note TEXT NOT NULL, status TEXT NOT NULL,"
                " leased_by TEXT, leased_at DOUBLE PRECISION,"
                " ok INTEGER, result TEXT, worker TEXT,"
                " created_at DOUBLE PRECISION NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_created"
                " ON tasks(status, created_at)"
            )
            conn.commit()

    def close(self) -> None:
        pass

    def enqueue(self, do: str, args: Mapping[str, str], note: str) -> str:
        task_id = self._ids()
        with _connect(self._dsn) as conn:
            conn.execute(
                'INSERT INTO tasks (id, "do", args, note, status, created_at)'
                " VALUES (%s, %s, %s, %s, 'pending', %s)",
                (task_id, do, json.dumps(dict(args)), note, self._now()),
            )
            conn.commit()
        return task_id

    def claim(self, worker: str, lease_seconds: float) -> LeasedTask | None:
        cutoff = self._now() - lease_seconds
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "UPDATE tasks SET status='leased', leased_by=%s, leased_at=%s"
                " WHERE id = ("
                "   SELECT id FROM tasks"
                "   WHERE status='pending' OR (status='leased' AND leased_at < %s)"
                "   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
                ' RETURNING id, "do", args, note',
                (worker, self._now(), cutoff),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return LeasedTask(
            id=row[0], do=row[1], args=json.loads(row[2]), note=row[3], worker=worker
        )

    def complete(
        self, task_id: str, worker: str, ok: bool, fields: Mapping[str, str], note: str
    ) -> bool:
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "UPDATE tasks SET status='done', ok=%s, result=%s, worker=%s, note=%s"
                " WHERE id=%s AND status <> 'done' RETURNING id",
                (1 if ok else 0, json.dumps(dict(fields)), worker, note, task_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def result(self, task_id: str) -> Result | None:
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT ok, result, worker, note FROM tasks"
                " WHERE id=%s AND status='done'",
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
        with _connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status <> 'done' ORDER BY created_at"
            ).fetchall()
        return [r[0] for r in rows]

    def pending_count(self) -> int:
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='pending'"
            ).fetchone()
        return int(row[0])


class PostgresChannelPolicyStore:
    """Shared A2A channel policies on Postgres, so every replica's bus authorizes
    channels identically."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with _connect(dsn) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS a2a_policies ("
                " channel TEXT PRIMARY KEY, post_scopes TEXT NOT NULL,"
                " read_scopes TEXT NOT NULL)"
            )
            conn.commit()

    def close(self) -> None:
        pass

    def declare(self, channel: str, policy: ChannelPolicy) -> None:
        with _connect(self._dsn) as conn:
            conn.execute(
                "INSERT INTO a2a_policies (channel, post_scopes, read_scopes)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (channel) DO UPDATE SET"
                " post_scopes = EXCLUDED.post_scopes,"
                " read_scopes = EXCLUDED.read_scopes",
                (
                    channel,
                    " ".join(sorted(policy.post_scopes)),
                    " ".join(sorted(policy.read_scopes)),
                ),
            )
            conn.commit()

    def get(self, channel: str) -> ChannelPolicy | None:
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT post_scopes, read_scopes FROM a2a_policies WHERE channel=%s",
                (channel,),
            ).fetchone()
        if row is None:
            return None
        return ChannelPolicy(
            post_scopes=frozenset(row[0].split()),
            read_scopes=frozenset(row[1].split()),
        )

    def channels(self) -> list[str]:
        with _connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT channel FROM a2a_policies ORDER BY channel"
            ).fetchall()
        return [r[0] for r in rows]


class PostgresVaultBackend:
    """Shared credential-ciphertext storage on Postgres. Holds only ciphertext
    (the vault owns the Fernet key), so a fleet shares one credential store
    without widening the trust boundary."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with _connect(dsn) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS credentials ("
                " subject TEXT NOT NULL, service TEXT NOT NULL,"
                " ciphertext BYTEA NOT NULL, updated_at DOUBLE PRECISION NOT NULL,"
                " PRIMARY KEY (subject, service))"
            )
            conn.commit()

    @property
    def durable(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def put(
        self, subject: str, service: str, ciphertext: bytes, updated_at: float
    ) -> None:
        with _connect(self._dsn) as conn:
            conn.execute(
                "INSERT INTO credentials (subject, service, ciphertext, updated_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (subject, service) DO UPDATE SET"
                " ciphertext = EXCLUDED.ciphertext, updated_at = EXCLUDED.updated_at",
                (subject, service, ciphertext, updated_at),
            )
            conn.commit()

    def get(self, subject: str, service: str) -> bytes | None:
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT ciphertext FROM credentials WHERE subject=%s AND service=%s",
                (subject, service),
            ).fetchone()
        return bytes(row[0]) if row is not None else None

    def delete(self, subject: str, service: str) -> bool:
        with _connect(self._dsn) as conn:
            cur = conn.execute(
                "DELETE FROM credentials WHERE subject=%s AND service=%s",
                (subject, service),
            )
            conn.commit()
            return int(cur.rowcount) > 0

    def services(self, subject: str) -> list[str]:
        with _connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT service FROM credentials WHERE subject=%s ORDER BY service",
                (subject,),
            ).fetchall()
        return [r[0] for r in rows]
