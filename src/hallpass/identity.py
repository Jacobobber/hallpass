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
from collections.abc import Callable
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
    identity provider says were granted. Nothing else is trusted.

    ``kind`` distinguishes a user-delegated token from a service (machine-to-
    machine, client-credentials) one, when the verifier is configured to tell
    them apart; it defaults to ``"user"``. It is descriptive, not a permission
    — access is still decided by scopes. Branch on ``is_service`` if a tool
    should behave differently for an agent acting as itself.
    """

    subject: str
    scopes: frozenset[str]
    claims: dict[str, Any] = field(repr=False, default_factory=dict)
    kind: str = "user"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"


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
    (used once on unknown kid, to pick up rotated keys).

    Stale-on-error: if a refresh fails (an IdP blip, a 5xx, a timeout) but a
    previously-fetched document is cached, the stale document is served rather
    than raising. Signing keys rotate slowly and overlap, so a slightly stale
    JWKS still verifies every token signed by a still-published key; failing
    instead would break verification across the whole fleet for a transient
    dependency outage. Only a cold cache (never fetched) re-raises. After a
    failed refresh, retries are throttled to ``error_retry_seconds`` so a
    verification storm during an outage does not hammer the IdP or block each
    call on the fetch timeout. This is also why readiness must not gate on a
    live JWKS fetch."""

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float = 300.0,
        error_retry_seconds: float = 30.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._error_retry = error_retry_seconds
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._last_ok = True

    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            fresh = now - self._fetched_at < self._ttl
            if self._cached is not None and fresh and not force_refresh:
                return self._cached
            # A refresh is due. If the last attempt failed recently, serve the
            # stale document without re-fetching, so an ongoing outage neither
            # hammers the IdP nor blocks every verify on the fetch timeout.
            if (
                self._cached is not None
                and not self._last_ok
                and now - self._last_attempt < self._error_retry
            ):
                return self._cached
            import httpx  # deferred: core installs work without httpx

            self._last_attempt = now
            try:
                response = httpx.get(self._url, timeout=10.0)
                response.raise_for_status()
                document: dict[str, Any] = response.json()
            except Exception:
                # Refresh failed. Serve the last good document if we have one;
                # only a cold cache has nothing to fall back on.
                self._last_ok = False
                if self._cached is not None:
                    return self._cached
                raise
            self._cached = document
            self._fetched_at = self._now()
            self._last_ok = True
            return document


class TokenVerifier:
    """Verify a bearer token and return the Principal it proves.

    Fail-closed by construction: any missing, malformed, expired,
    mis-audienced, mis-issued, unsigned, or unknown-key token raises
    VerificationError. There is no "best effort" path.
    """

    _ALLOWED_ALGORITHMS = ["RS256"]  # asymmetric only; HS* and none are refused

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: JwksSource,
        service_claim: str | None = None,
        service_values: frozenset[str] = frozenset(),
        cache_size: int = 2048,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        # When set, a token whose ``service_claim`` value is in ``service_values``
        # verifies as a service principal (e.g. Auth0's gty=client-credentials,
        # Azure's idtyp=app). Left unset, every principal is a user.
        self._service_claim = service_claim
        self._service_values = service_values
        # Verified-token cache: a token verifies identically until its own exp
        # (JWT verification checks no revocation list), so caching the Principal
        # keyed by the raw token until exp skips the RSA signature check on the
        # hot path. Bounded; set cache_size=0 to disable.
        self._cache_size = cache_size
        self._cache: dict[str, tuple[Principal, float]] = {}
        self._cache_lock = threading.Lock()

    def verify(self, token: str) -> Principal:
        if not token:
            raise VerificationError("no bearer token presented")
        if self._cache_size:
            with self._cache_lock:
                cached = self._cache.get(token)
                if cached is not None and time.time() < cached[1]:
                    return cached[0]
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

        # `require` proves the claim is present, not that it is meaningful.
        # Everything downstream partitions on the subject, so a blank one
        # would collapse identities; refuse it.
        if not claims["sub"]:
            raise VerificationError("token subject is empty")

        kind = "user"
        if (
            self._service_claim is not None
            and str(claims.get(self._service_claim)) in self._service_values
        ):
            kind = "service"

        principal = Principal(
            subject=claims["sub"],
            scopes=frozenset(_extract_scopes(claims)),
            claims=claims,
            kind=kind,
        )
        if self._cache_size:
            with self._cache_lock:
                if len(self._cache) >= self._cache_size:
                    self._cache.pop(next(iter(self._cache)))  # bounded: drop oldest
                self._cache[token] = (principal, float(claims["exp"]))
        return principal

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
