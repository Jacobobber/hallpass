"""Roles: named capability sets assignable to principals.

A harness (see ``hallpass.agents``) is the scope ceiling of an agent *type*; a
role is a named scope set assigned to a *principal* (agent or human), and a
subject's effective scopes are the union of the roles it holds. This is the
governance substrate for the org: "membership in a team" is "holding a role",
and what a role can do is a scope set, decided by the same gate that guards a
single tool call. Resolve a subject's effective scopes with ``scopes_for`` and
mint its token with exactly those, so an org change is a role change, not a
per-agent scope edit.

``InMemoryRoleStore`` is the single-process default (thread-safe, lost on
restart); ``SqliteRoleStore`` persists roles and assignments behind the same
protocol, mirroring the vault/consent storage pattern (one connection, WAL, one
lock).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Protocol

__all__ = ["Role", "RoleError", "RoleStore", "InMemoryRoleStore", "SqliteRoleStore"]


class RoleError(KeyError):
    """A role was assigned before it was defined. A subclass of ``KeyError`` so
    existing ``except KeyError`` handling still catches it; the message says
    which role and what to do."""


@dataclass(frozen=True)
class Role:
    """A named capability set. Assigning it to a subject grants those scopes."""

    name: str
    scopes: frozenset[str] = frozenset()


class RoleStore(Protocol):
    def define(self, role: Role) -> None:
        """Create or replace a role's scope set."""
        ...

    def assign(self, subject: str, role: str) -> None:
        """Give ``subject`` a role. Raises ``RoleError`` if the role is not
        defined -- assigning an unknown role is a misconfiguration, not a
        silent empty grant."""
        ...

    def unassign(self, subject: str, role: str) -> bool:
        """Remove a role from a subject. True if it held it."""
        ...

    def roles(self, subject: str) -> list[str]:
        """The role names this subject holds, sorted."""
        ...

    def scopes_for(self, subject: str) -> frozenset[str]:
        """The subject's effective scopes: the union of its roles' scopes. Mint
        the subject's token with exactly these."""
        ...


class InMemoryRoleStore:
    """Single-process role store; thread-safe, not durable. Production wires a
    ``SqliteRoleStore`` (or another backend) behind the same protocol."""

    def __init__(self) -> None:
        self._defs: dict[str, frozenset[str]] = {}
        self._assignments: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def define(self, role: Role) -> None:
        with self._lock:
            self._defs[role.name] = frozenset(role.scopes)

    def assign(self, subject: str, role: str) -> None:
        with self._lock:
            if role not in self._defs:
                raise RoleError(
                    f"role {role!r} is not defined; define it before assigning it"
                )
            self._assignments.setdefault(subject, set()).add(role)

    def unassign(self, subject: str, role: str) -> bool:
        with self._lock:
            held = self._assignments.get(subject)
            if held is None or role not in held:
                return False
            held.discard(role)
            return True

    def roles(self, subject: str) -> list[str]:
        with self._lock:
            return sorted(self._assignments.get(subject, set()))

    def scopes_for(self, subject: str) -> frozenset[str]:
        with self._lock:
            out: set[str] = set()
            for role in self._assignments.get(subject, set()):
                out |= self._defs.get(role, frozenset())
            return frozenset(out)


class SqliteRoleStore:
    """A durable role store backed by SQLite, so role definitions and
    assignments survive a restart. Pass a file ``path`` to persist and share
    across instances; ``:memory:`` is a single-process fallback."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS roles ("
                " name TEXT PRIMARY KEY, scopes TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS role_assignments ("
                " subject TEXT NOT NULL, role TEXT NOT NULL,"
                " PRIMARY KEY (subject, role))"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_role_assignments_subject"
                " ON role_assignments(subject)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def define(self, role: Role) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO roles (name, scopes) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET scopes = excluded.scopes",
                (role.name, " ".join(sorted(role.scopes))),
            )

    def assign(self, subject: str, role: str) -> None:
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM roles WHERE name = ?", (role,)
            ).fetchone()
            if exists is None:
                raise RoleError(
                    f"role {role!r} is not defined; define it before assigning it"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO role_assignments (subject, role) VALUES (?, ?)",
                (subject, role),
            )

    def unassign(self, subject: str, role: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM role_assignments WHERE subject = ? AND role = ?",
                (subject, role),
            )
            return cur.rowcount > 0

    def roles(self, subject: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role FROM role_assignments WHERE subject = ? ORDER BY role",
                (subject,),
            ).fetchall()
        return [r[0] for r in rows]

    def scopes_for(self, subject: str) -> frozenset[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.scopes FROM role_assignments a"
                " JOIN roles r ON r.name = a.role"
                " WHERE a.subject = ?",
                (subject,),
            ).fetchall()
        out: set[str] = set()
        for (scopes,) in rows:
            out |= set(scopes.split()) if scopes else set()
        return frozenset(out)
