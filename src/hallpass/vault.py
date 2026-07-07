"""Per-user encrypted storage for downstream service credentials.

A multi-user tool server holds other people's API keys; this is the most
sensitive thing it does. The rules: every secret is encrypted at rest
(Fernet, AES-128-CBC + HMAC), keyed by (user subject, service) so no code
path can read across users by accident, and no secret ever appears in a
repr, a log line, or an exception message. The Fernet key comes from the
operator (env var, KMS, file); the vault never generates or persists it.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["CredentialVault", "VaultError"]


class VaultError(Exception):
    """Raised when a stored credential cannot be decrypted (wrong or
    rotated key). Message carries no secret material."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    subject    TEXT NOT NULL,
    service    TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (subject, service)
);
"""


class CredentialVault:
    def __init__(self, fernet_key: str | bytes, *, path: str = ":memory:") -> None:
        self._fernet = Fernet(fernet_key)
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    @property
    def durable(self) -> bool:
        """True when credentials are backed by a file that survives a restart;
        False for the in-memory default. A diagnostic reads this to warn."""
        return self._path != ":memory:"

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def store(self, subject: str, service: str, secret: str) -> None:
        ciphertext = self._fernet.encrypt(secret.encode("utf-8"))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO credentials (subject, service, ciphertext, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subject, service) DO UPDATE"
                " SET ciphertext = excluded.ciphertext, updated_at = excluded.updated_at",
                (subject, service, ciphertext, time.time()),
            )

    def fetch(self, subject: str, service: str) -> str | None:
        """The stored secret, or None when the user has not connected the
        service. Absence is a normal state, not an error."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM credentials WHERE subject = ? AND service = ?",
                (subject, service),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken:
            raise VaultError(
                f"credential for service {service!r} cannot be decrypted"
                " (encryption key changed?)"
            ) from None

    def delete(self, subject: str, service: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM credentials WHERE subject = ? AND service = ?",
                (subject, service),
            )
            return cur.rowcount > 0

    def services(self, subject: str) -> list[str]:
        """Which services this user has connected. Names only; never the
        credentials themselves."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT service FROM credentials WHERE subject = ? ORDER BY service",
                (subject,),
            ).fetchall()
            return [r[0] for r in rows]

    def __repr__(self) -> str:  # never leak contents through debug output
        return "CredentialVault(<locked>)"
