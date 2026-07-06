"""Token verification: who is this, and what were they actually granted.

The verifier is deliberately boring OAuth 2.1 resource-server behavior:
RS256 JWTs checked against a JWKS, with exact issuer and audience matches
and a fail-closed answer to every ambiguity. The JWKS source is injected,
so production fetches an OIDC provider's keys over HTTPS while tests hand
in a static document -- verification logic stays identical and the test
suite needs no network.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt

__all__ = [
    "Principal",
    "JwksSource",
    "StaticJwks",
    "HttpJwks",
    "TokenVerifier",
    "VerificationError",
]


class VerificationError(Exception):
    """The token was rejected. The message is safe to log: it never
    contains the token or any claim values."""


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: a stable subject and the scopes the
    identity provider says were granted. Nothing else is trusted."""

    subject: str
    scopes: frozenset[str]
    claims: dict[str, Any] = field(repr=False, default_factory=dict)


class JwksSource(Protocol):
    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a JWKS document ({"keys": [...]})."""
        ...


class StaticJwks:
    """A fixed JWKS document. For tests and air-gapped setups."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return self._document


class HttpJwks:
    """JWKS over HTTPS with a TTL cache. force_refresh bypasses the cache
    (used once on unknown kid, to pick up rotated keys)."""

    def __init__(self, url: str, *, ttl_seconds: float = 300.0) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._fetched_at = 0.0

    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < self._ttl
            if self._cached is not None and fresh and not force_refresh:
                return self._cached
            import httpx  # deferred: core installs work without httpx

            response = httpx.get(self._url, timeout=10.0)
            response.raise_for_status()
            document: dict[str, Any] = response.json()
            self._cached = document
            self._fetched_at = time.monotonic()
            return document


class TokenVerifier:
    """Verify a bearer token and return the Principal it proves.

    Fail-closed by construction: any missing, malformed, expired,
    mis-audienced, mis-issued, unsigned, or unknown-key token raises
    VerificationError. There is no "best effort" path.
    """

    _ALLOWED_ALGORITHMS = ["RS256"]  # asymmetric only; HS* and none are refused

    def __init__(self, *, issuer: str, audience: str, jwks: JwksSource) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks

    def verify(self, token: str) -> Principal:
        if not token:
            raise VerificationError("no bearer token presented")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise VerificationError(
                f"malformed token header: {type(exc).__name__}"
            ) from None

        if header.get("alg") not in self._ALLOWED_ALGORITHMS:
            raise VerificationError("disallowed signing algorithm")

        key = self._key_for(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            # Exception type only: claim values and the token itself must
            # never reach logs through this error.
            raise VerificationError(f"token rejected: {type(exc).__name__}") from None

        return Principal(
            subject=claims["sub"],
            scopes=frozenset(_extract_scopes(claims)),
            claims=claims,
        )

    def _key_for(self, kid: str | None) -> Any:
        if kid is None:
            raise VerificationError("token header has no kid")
        jwk = _find_key(self._jwks.get(), kid)
        if jwk is None:
            # One refresh to pick up a rotated key, then fail closed.
            jwk = _find_key(self._jwks.get(force_refresh=True), kid)
        if jwk is None:
            raise VerificationError("no JWKS key matches the token kid")
        return jwt.PyJWK(jwk).key


def _find_key(document: dict[str, Any], kid: str) -> dict[str, Any] | None:
    for jwk in document.get("keys", []):
        if jwk.get("kid") == kid:
            return dict(jwk)
    return None


def _extract_scopes(claims: dict[str, Any]) -> list[str]:
    """Accept the two shapes identity providers actually emit: a
    space-delimited "scope" string (RFC 8693 / OAuth) or an "scp" list."""
    scope = claims.get("scope")
    if isinstance(scope, str):
        return scope.split()
    scp = claims.get("scp")
    if isinstance(scp, list):
        return [s for s in scp if isinstance(s, str)]
    if isinstance(scp, str):
        return scp.split()
    return []
