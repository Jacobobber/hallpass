"""Per-provider OAuth connect flow: a user connects a service and its access
token lands in the vault, so the catalog's connectors work end to end.

This is the piece that turns "N declared connectors" into "a user connects
and it works." hallpass provides the transport-agnostic parts and never
touches a browser:

- ``start(subject, service)`` returns the provider's authorize URL, carrying
  a single-use ``state`` (CSRF) and a PKCE challenge.
- ``finish(state, code)`` validates the state, exchanges the code for tokens,
  and stores the access token in the vault under the service (where the
  connector reads it), plus a refresh bundle alongside.
- ``refresh(subject, service)`` renews an expired access token.

The operator wires start/finish to their own redirect routes and supplies the
OAuth client credentials. Tokens, secrets, and codes never appear in a log or
an error message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from .consent import Consent, ConsentLedger
from .vault import CredentialVault

__all__ = [
    "OAuthProvider",
    "OAuthConnect",
    "OAuthError",
    "PendingStore",
    "InMemoryPendingStore",
    "SqlitePendingStore",
    "TokenHttp",
    "HttpxTokenClient",
]


class OAuthError(Exception):
    """An OAuth step failed: unknown/expired state, or the provider rejected
    the exchange. The message never contains a token, code, or secret."""


@dataclass(frozen=True)
class OAuthProvider:
    authorize_url: str
    token_url: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = None  # omit for a public (PKCE-only) client
    scopes: tuple[str, ...] = ()
    use_pkce: bool = True
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Pending:
    subject: str
    service: str
    code_verifier: str
    created_at: float
    scopes: tuple[str, ...] = ()


class PendingStore(Protocol):
    def put(self, state: str, pending: _Pending) -> None: ...
    def pop(self, state: str) -> _Pending | None:
        """Return and REMOVE the pending record (single-use), or None."""
        ...


class InMemoryPendingStore:
    """Single-process pending-auth store with a TTL. Production behind a
    load balancer wires a shared store (Redis, a table) via the protocol."""

    def __init__(
        self, *, ttl_seconds: float = 600.0, now: Callable[[], float] = time.time
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._pending: dict[str, _Pending] = {}

    def put(self, state: str, pending: _Pending) -> None:
        self._pending[state] = pending

    def pop(self, state: str) -> _Pending | None:
        record = self._pending.pop(state, None)
        if record is None:
            return None
        if self._now() - record.created_at > self._ttl:
            return None
        return record


class SqlitePendingStore:
    """A PendingStore backed by SQLite, so OAuth ``start`` and ``finish`` can
    land on different instances behind a load balancer. State is single-use
    (deleted on pop, atomically) and expires by TTL. Pass a file ``path`` for
    cross-process sharing; ``:memory:`` is a single-process fallback."""

    def __init__(
        self,
        *,
        path: str = ":memory:",
        ttl_seconds: float = 600.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        import sqlite3

        self._ttl = ttl_seconds
        self._now = now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        # WAL uniformly across the SQLite-backed stores (no-op on :memory:).
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS oauth_pending ("
                " state TEXT PRIMARY KEY, subject TEXT NOT NULL,"
                " service TEXT NOT NULL, code_verifier TEXT NOT NULL,"
                " created_at REAL NOT NULL, scopes TEXT NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def put(self, state: str, pending: _Pending) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_pending"
                " (state, subject, service, code_verifier, created_at, scopes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    state,
                    pending.subject,
                    pending.service,
                    pending.code_verifier,
                    pending.created_at,
                    " ".join(pending.scopes),
                ),
            )

    def pop(self, state: str) -> _Pending | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT subject, service, code_verifier, created_at, scopes"
                    " FROM oauth_pending WHERE state = ?",
                    (state,),
                ).fetchone()
                if row is not None:
                    # single-use: remove it whether or not it has expired
                    self._conn.execute(
                        "DELETE FROM oauth_pending WHERE state = ?", (state,)
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        if row is None:
            return None
        subject, service, verifier, created_at, scopes = row
        if self._now() - float(created_at) > self._ttl:
            return None
        return _Pending(
            subject=subject,
            service=service,
            code_verifier=verifier,
            created_at=float(created_at),
            scopes=tuple(scopes.split()) if scopes else (),
        )


class TokenHttp(Protocol):
    def post_form(
        self, url: str, *, data: dict[str, str], headers: dict[str, str]
    ) -> Any:
        """Form-encoded POST to a token endpoint; return the parsed JSON."""
        ...


class _AutoRefreshable(Protocol):
    """A connector that can be wired for seamless token refresh."""

    service: str

    def set_auto_refresh(
        self, refresher: Callable[[str, str], object] | None
    ) -> None: ...


class HttpxTokenClient:
    """Default token-exchange client over httpx (the ``connectors`` extra)."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def post_form(
        self, url: str, *, data: dict[str, str], headers: dict[str, str]
    ) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise OAuthError(
                "the OAuth flow needs the 'connectors' extra: "
                "pip install 'hallpass[connectors]'"
            ) from exc
        response = httpx.post(url, data=data, headers=headers, timeout=self._timeout)
        if response.status_code >= 400:
            raise OAuthError(f"token endpoint returned HTTP {response.status_code}")
        return response.json()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


class OAuthConnect:
    """Drives the authorization-code flow and stores tokens in the vault."""

    def __init__(
        self,
        *,
        vault: CredentialVault,
        providers: dict[str, OAuthProvider],
        token_http: TokenHttp | None = None,
        pending: PendingStore | None = None,
        consent: ConsentLedger | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._vault = vault
        self._providers = providers
        self._http = token_http or HttpxTokenClient()
        self._pending = pending or InMemoryPendingStore(now=now)
        self._consent = consent
        self._now = now

    def _provider(self, service: str) -> OAuthProvider:
        provider = self._providers.get(service)
        if provider is None:
            raise OAuthError(f"no OAuth provider configured for {service!r}")
        return provider

    def start(
        self, subject: str, service: str, *, scopes: Iterable[str] | None = None
    ) -> str:
        """Return the provider authorize URL for this user to visit. A fresh
        single-use state and PKCE verifier are recorded for finish()."""
        provider = self._provider(service)
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair() if provider.use_pkce else ("", "")
        wanted = tuple(scopes) if scopes is not None else provider.scopes
        self._pending.put(
            state, _Pending(subject, service, verifier, self._now(), wanted)
        )
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "state": state,
        }
        if wanted:
            params["scope"] = " ".join(wanted)
        if provider.use_pkce:
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        params.update(provider.extra_authorize_params)
        sep = "&" if "?" in provider.authorize_url else "?"
        return f"{provider.authorize_url}{sep}{urlencode(params)}"

    def finish(self, state: str, code: str) -> str:
        """Validate the state, exchange the code, store the tokens, and
        return the subject that connected."""
        pending = self._pending.pop(state)
        if pending is None:
            raise OAuthError("unknown or expired OAuth state")
        provider = self._provider(pending.service)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "client_id": provider.client_id,
        }
        if provider.client_secret:
            data["client_secret"] = provider.client_secret
        if provider.use_pkce:
            data["code_verifier"] = pending.code_verifier
        tokens = self._http.post_form(
            provider.token_url, data=data, headers={"Accept": "application/json"}
        )
        self._store_tokens(pending.subject, pending.service, tokens)
        if self._consent is not None:
            # The scope the provider actually granted, if it echoed one; else
            # what the user was sent to authorize.
            granted = tokens.get("scope")
            scopes = tuple(granted.split()) if granted else pending.scopes
            self._consent.grant(
                pending.subject, pending.service, scopes, at=self._now()
            )
        return pending.subject

    def refresh(self, subject: str, service: str) -> str:
        """Use the stored refresh token to get a new access token; update the
        vault and return the new access token."""
        provider = self._provider(service)
        bundle = self._vault.fetch(subject, self._oauth_slot(service))
        refresh_token = json.loads(bundle).get("refresh_token") if bundle else None
        if not refresh_token:
            raise OAuthError(f"no refresh token stored for {service!r}")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": provider.client_id,
        }
        if provider.client_secret:
            data["client_secret"] = provider.client_secret
        tokens = self._http.post_form(
            provider.token_url, data=data, headers={"Accept": "application/json"}
        )
        # A refresh response may omit a new refresh token; keep the old one.
        tokens.setdefault("refresh_token", refresh_token)
        self._store_tokens(subject, service, tokens)
        return str(tokens["access_token"])

    def valid_token(self, subject: str, service: str, *, leeway: float = 60.0) -> str:
        """Return a currently-valid access token, refreshing first if the
        stored one is expired (or within ``leeway`` seconds of it). Use this
        before a call when you would rather refresh proactively than let the
        call 401. Raises if the service was never connected."""
        access = self._vault.fetch(subject, service)
        if access is None:
            raise OAuthError(f"{service!r} is not connected for {subject!r}")
        bundle_raw = self._vault.fetch(subject, self._oauth_slot(service))
        bundle = json.loads(bundle_raw) if bundle_raw else {}
        expires_at = bundle.get("expires_at")
        expiring = expires_at is not None and self._now() + leeway >= float(expires_at)
        if expiring and bundle.get("refresh_token"):
            return self.refresh(subject, service)
        return access

    def attach_refresh(self, *connectors: _AutoRefreshable) -> None:
        """Wire this flow's ``refresh`` into each connector so a stale token
        renews transparently on a 401/403 and the call retries once. The one
        line that makes OAuth connectors self-healing."""
        for connector in connectors:
            connector.set_auto_refresh(self.refresh)

    def disconnect(self, subject: str, service: str) -> bool:
        """Revoke a connection: forget the access token AND the refresh bundle,
        and drop the consent record. Returns True if anything was removed. The
        counterpart to a successful ``finish``; this is how a user takes their
        credentials back. (It does not call the provider's own revoke endpoint;
        wire that separately if the provider offers one.)"""
        removed = self._vault.delete(subject, service)
        removed = self._vault.delete(subject, self._oauth_slot(service)) or removed
        if self._consent is not None:
            removed = self._consent.revoke(subject, service) or removed
        return removed

    def consents(self, subject: str) -> list[Consent]:
        """Every service this user has an active consent for. Requires a
        consent ledger; without one, returns an empty list."""
        return self._consent.list(subject) if self._consent is not None else []

    @staticmethod
    def _oauth_slot(service: str) -> str:
        return f"{service}:oauth"

    def _store_tokens(self, subject: str, service: str, tokens: Any) -> None:
        access = tokens.get("access_token")
        if not access:
            raise OAuthError("token response had no access_token")
        # The connector reads the raw access token from the service slot.
        self._vault.store(subject, service, str(access))
        expires_in = tokens.get("expires_in")
        bundle = {
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": (self._now() + float(expires_in)) if expires_in else None,
            "scope": tokens.get("scope"),
        }
        self._vault.store(subject, self._oauth_slot(service), json.dumps(bundle))
