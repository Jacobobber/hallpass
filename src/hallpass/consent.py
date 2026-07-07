"""A record of what each user has connected, so it can be shown and revoked.

The vault holds the secret; this holds the *fact* of the grant -- which
service, which scopes, and when. A multi-user server needs both: "here is
everything you have connected, revoke any of it" is table stakes for handing
your credentials to someone else's software, and it is not something the raw
credential store answers (it deliberately knows only ciphertext).

The ledger is injected, so a single process uses the in-memory default and a
real deployment backs it with the same database as the rest of its state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

__all__ = ["Consent", "ConsentLedger", "InMemoryConsentLedger"]


@dataclass(frozen=True)
class Consent:
    """One user's active grant for one service."""

    subject: str
    service: str
    scopes: tuple[str, ...]
    granted_at: float


class ConsentLedger(Protocol):
    def grant(
        self, subject: str, service: str, scopes: Iterable[str], *, at: float
    ) -> None:
        """Record (or replace) the user's consent for a service."""
        ...

    def get(self, subject: str, service: str) -> Consent | None: ...

    def list(self, subject: str) -> list[Consent]:
        """Every service this user has an active consent for."""
        ...

    def revoke(self, subject: str, service: str) -> bool:
        """Remove the consent record. True if one existed."""
        ...


class InMemoryConsentLedger:
    """Single-process consent ledger. Production wires a durable one behind
    the same protocol (a table alongside the vault)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], Consent] = {}

    def grant(
        self, subject: str, service: str, scopes: Iterable[str], *, at: float
    ) -> None:
        self._records[(subject, service)] = Consent(
            subject=subject, service=service, scopes=tuple(scopes), granted_at=at
        )

    def get(self, subject: str, service: str) -> Consent | None:
        return self._records.get((subject, service))

    def list(self, subject: str) -> list[Consent]:
        return sorted(
            (c for (s, _), c in self._records.items() if s == subject),
            key=lambda c: c.service,
        )

    def revoke(self, subject: str, service: str) -> bool:
        return self._records.pop((subject, service), None) is not None
