"""Batteries-included setup: one call to a working server.

``build`` assembles a ready ``Hallpass`` from minimal configuration.
``dev_app`` goes further for local development and demos: it generates a
signing key, wires a static JWKS, and hands back a token minter, so you can
exercise a fully gated, per-user server without standing up an identity
provider.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable

from cryptography.fernet import Fernet

from .audit import AuditSink
from .connectors import Connector
from .core import Hallpass
from .identity import HttpJwks, JwksSource, StaticJwks, TokenVerifier
from .idempotency import IdempotencyStore
from .ratelimit import FixedWindowRateLimiter, RateLimiter
from .vault import CredentialVault

__all__ = ["build", "dev_app"]


def build(
    *,
    issuer: str,
    audience: str,
    jwks_url: str | None = None,
    jwks: JwksSource | None = None,
    vault_key: str | bytes | None = None,
    vault_path: str = ":memory:",
    audit: AuditSink | None = None,
    rate_limit: tuple[int, float] | None = None,
    idempotency: IdempotencyStore | None = None,
    connectors: Iterable[Connector] = (),
) -> Hallpass:
    """Assemble a ready Hallpass from minimal configuration.

    Provide either ``jwks_url`` (your OIDC provider's JWKS endpoint, fetched
    over HTTPS) or a ``jwks`` source. ``vault_key`` is the Fernet key for the
    per-user credential store; if omitted a fresh one is generated, which is
    fine for a single process but must be supplied to survive restarts.
    ``rate_limit`` is ``(max_calls, window_seconds)``. Connectors are
    registered in order.
    """
    if jwks is None:
        if not jwks_url:
            raise ValueError("provide either jwks_url or a jwks source")
        jwks = HttpJwks(jwks_url)
    verifier = TokenVerifier(issuer=issuer, audience=audience, jwks=jwks)
    vault = CredentialVault(vault_key or Fernet.generate_key(), path=vault_path)
    limiter: RateLimiter | None = None
    if rate_limit is not None:
        limiter = FixedWindowRateLimiter(rate_limit[0], rate_limit[1])
    app = Hallpass(
        verifier=verifier,
        vault=vault,
        audit=audit,
        rate_limiter=limiter,
        idempotency=idempotency,
    )
    for connector in connectors:
        app.add_connector(connector)
    return app


def dev_app(
    *,
    connectors: Iterable[Connector] = (),
    issuer: str = "https://hallpass.dev",
    audience: str = "https://hallpass.dev/api",
) -> tuple[Hallpass, Callable[..., str]]:
    """A zero-config app for local development and demos.

    Generates an in-memory RSA keypair, wires a static JWKS, and returns the
    app plus a token minter, so you can call gated tools immediately:

        app, token = dev_app(connectors=[kit])
        app.call_tool(token("alice", ["notes:read"]), "read_note", {"id": "1"})

    NOT for production: the signing key lives in this process and the minter
    will sign a token for any subject and scopes you ask for.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    import jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="dev", use="sig", alg="RS256")
    app = build(
        issuer=issuer,
        audience=audience,
        jwks=StaticJwks({"keys": [jwk]}),
        connectors=connectors,
    )

    def token(subject: str, scopes: Iterable[str] = ()) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dev"})

    return app, token
