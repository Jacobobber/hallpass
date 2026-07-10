"""Per-user encrypted storage for downstream service credentials.

A multi-user tool server holds other people's API keys; this is the most
sensitive thing it does. The rules: every secret is encrypted at rest
(Fernet, AES-128-CBC + HMAC), keyed by (user subject, service) so no code
path can read across users by accident, and no secret ever appears in a
repr, a log line, or an exception message. The Fernet key comes from the
operator (env var, KMS, file); the vault never generates or persists it.

``CredentialVault`` owns the encryption; *where* the ciphertext is stored is a
``VaultBackend`` -- SQLite by default (``SqliteVaultBackend``), or a shared
database / KMS-backed store for a multi-replica deployment (the one credential
store that had no swappable backend before). The backend only ever sees
ciphertext; the encryption boundary stays in this module regardless of where the
bytes land.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "CredentialVault",
    "VaultError",
    "VaultBackend",
    "InMemoryVaultBackend",
    "SqliteVaultBackend",
]


class VaultError(Exception):
    """Raised when a stored credential cannot be decrypted (wrong or
    rotated key). Message carries no secret material."""


class VaultBackend(Protocol):
    """Raw ciphertext storage for the vault, keyed by (subject, service). The
    backend never sees a plaintext secret or the encryption key -- only opaque
    bytes -- so a shared-database or KMS backend can hold credentials without
    widening the trust boundary. The default is ``SqliteVaultBackend``."""

    @property
    def durable(self) -> bool:
        """True when writes survive a restart (a file, a server); False for an
        in-memory backend. A diagnostic reads this to warn."""
        ...

    def put(
        self, subject: str, service: str, ciphertext: bytes, updated_at: float
    ) -> None: ...
    def get(self, subject: str, service: str) -> bytes | None: ...
    def delete(self, subject: str, service: str) -> bool: ...
    def services(self, subject: str) -> list[str]: ...
    def close(self) -> None: ...


class InMemoryVaultBackend:
    """Ciphertext in a process-local dict; thread-safe, not durable. For tests
    and single-process ephemeral use."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self._lock = threading.Lock()

    @property
    def durable(self) -> bool:
        return False

    def put(
        self, subject: str, service: str, ciphertext: bytes, updated_at: float
    ) -> None:
        with self._lock:
            self._data[(subject, service)] = ciphertext

    def get(self, subject: str, service: str) -> bytes | None:
        with self._lock:
            return self._data.get((subject, service))

    def delete(self, subject: str, service: str) -> bool:
        with self._lock:
            return self._data.pop((subject, service), None) is not None

    def services(self, subject: str) -> list[str]:
        with self._lock:
            return sorted(svc for (sub, svc) in self._data if sub == subject)

    def close(self) -> None:
        pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    subject    TEXT NOT NULL,
    service    TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (subject, service)
);
"""


class SqliteVaultBackend:
    """Ciphertext in SQLite (the vault default). Pass a file ``path`` to survive
    a restart; ``:memory:`` is the single-process default. WAL, one connection,
    one lock -- the shared storage pattern."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    @property
    def durable(self) -> bool:
        return self._path != ":memory:"

    def put(
        self, subject: str, service: str, ciphertext: bytes, updated_at: float
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO credentials (subject, service, ciphertext, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subject, service) DO UPDATE"
                " SET ciphertext = excluded.ciphertext, updated_at = excluded.updated_at",
                (subject, service, ciphertext, updated_at),
            )

    def get(self, subject: str, service: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM credentials WHERE subject = ? AND service = ?",
                (subject, service),
            ).fetchone()
        return row[0] if row is not None else None

    def delete(self, subject: str, service: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM credentials WHERE subject = ? AND service = ?",
                (subject, service),
            )
            return cur.rowcount > 0

    def services(self, subject: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT service FROM credentials WHERE subject = ? ORDER BY service",
                (subject,),
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class CredentialVault:
    """Fernet-encrypted per-(subject, service) credential storage. Owns the
    encryption; delegates ciphertext storage to a ``VaultBackend`` (SQLite by
    default). The public API is unchanged from before the backend seam existed:
    pass a ``path`` for the SQLite default, or a ``backend`` for anything else."""

    def __init__(
        self,
        fernet_key: str | bytes,
        *,
        path: str = ":memory:",
        backend: VaultBackend | None = None,
    ) -> None:
        self._fernet = Fernet(fernet_key)
        self._backend: VaultBackend = backend or SqliteVaultBackend(path=path)

    @property
    def durable(self) -> bool:
        """True when credentials are backed by storage that survives a restart;
        False for the in-memory default. A diagnostic reads this to warn."""
        return self._backend.durable

    def close(self) -> None:
        self._backend.close()

    def store(self, subject: str, service: str, secret: str) -> None:
        ciphertext = self._fernet.encrypt(secret.encode("utf-8"))
        self._backend.put(subject, service, ciphertext, time.time())

    def fetch(self, subject: str, service: str) -> str | None:
        """The stored secret, or None when the user has not connected the
        service. Absence is a normal state, not an error."""
        ciphertext = self._backend.get(subject, service)
        if ciphertext is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise VaultError(
                f"credential for service {service!r} cannot be decrypted"
                " (encryption key changed?)"
            ) from None

    def delete(self, subject: str, service: str) -> bool:
        return self._backend.delete(subject, service)

    def services(self, subject: str) -> list[str]:
        """Which services this user has connected. Names only; never the
        credentials themselves."""
        return self._backend.services(subject)

    def __repr__(self) -> str:  # never leak contents through debug output
        return "CredentialVault(<locked>)"
