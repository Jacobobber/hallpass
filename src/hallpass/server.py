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
import warnings
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
    service_claim: str | None = None,
    service_values: frozenset[str] = frozenset(),
    connectors: Iterable[Connector] = (),
    database_url: str | None = None,
    redis_url: str | None = None,
) -> Hallpass:
    """Assemble a ready Hallpass from minimal configuration.

    Provide either ``jwks_url`` (your OIDC provider's JWKS endpoint, fetched
    over HTTPS) or a ``jwks`` source. ``vault_key`` is the Fernet key for the
    per-user credential store; if omitted a fresh one is generated, which is
    fine for a single process but must be supplied to survive restarts.
    ``rate_limit`` is ``(max_calls, window_seconds)``. ``service_claim`` /
    ``service_values`` mark a token as a service (machine-to-machine) principal
    (e.g. Auth0 ``gty=client-credentials``, Azure ``idtyp=app``) — needed if you
    guard spawned agents as service identities. Connectors are registered in
    order.

    **Backend selection for a multi-replica rollout.** Pass ``database_url`` to
    store credential ciphertext in shared Postgres (the ``postgres`` extra), so
    every replica reads one vault; the Fernet key stays in this process and the
    backend only ever sees ciphertext. Pass ``redis_url`` to share idempotency
    and rate limiting across replicas (the ``redis`` extra) — the in-process
    defaults fail silently behind a load balancer (a retry that lands elsewhere
    re-runs the mutation; the per-subject budget becomes N× the cap). With
    neither, the app is single-node on SQLite/in-memory (unchanged default). An
    explicitly passed ``idempotency`` store always wins over ``redis_url``.
    """
    if jwks is None:
        if not jwks_url:
            raise ValueError("provide either jwks_url or a jwks source")
        jwks = HttpJwks(jwks_url)
    verifier = TokenVerifier(
        issuer=issuer,
        audience=audience,
        jwks=jwks,
        service_claim=service_claim,
        service_values=service_values,
    )
    if database_url and not vault_key:
        # A shared Postgres vault holds ciphertext; if each replica generated its
        # own ephemeral Fernet key, replica A's writes would be undecryptable on
        # replica B (VaultError) -- silent credential corruption. Fail closed:
        # a shared vault requires a stable key. This is fatal regardless of any
        # "allow ephemeral" escape hatch.
        raise ValueError(
            "database_url is set but vault_key is not. A shared vault needs a "
            "stable key (set HALLPASS_VAULT_KEY) or each replica mints a "
            "different Fernet key and cannot read another replica's credentials."
        )
    if database_url and not redis_url:
        # The vault is shared but idempotency/rate-limiting stay per-process,
        # which is wrong for more than one replica (a retry on another replica
        # re-runs the mutation; the per-subject budget becomes N x the cap).
        # A single-node durable-vault deployment is legitimate, so warn rather
        # than fail; the multi-replica topology guard lives in the deploy layer.
        warnings.warn(
            "database_url without redis_url: the vault is shared but idempotency "
            "and rate limiting are per-process. Set redis_url for a multi-replica "
            "deployment, or ignore this on a single node.",
            stacklevel=2,
        )
    key = vault_key or Fernet.generate_key()
    if database_url:
        # Deferred so a core install without the postgres extra is unaffected.
        from .postgres_backends import PostgresVaultBackend

        vault = CredentialVault(key, backend=PostgresVaultBackend(database_url))
    else:
        vault = CredentialVault(key, path=vault_path)
    idem = idempotency
    if idem is None and redis_url:
        from .redis_backends import RedisIdempotencyStore

        idem = RedisIdempotencyStore.from_url(redis_url)
    limiter: RateLimiter | None = None
    if rate_limit is not None:
        if redis_url:
            from .redis_backends import RedisRateLimiter

            limiter = RedisRateLimiter.from_url(rate_limit[0], rate_limit[1], redis_url)
        else:
            limiter = FixedWindowRateLimiter(rate_limit[0], rate_limit[1])
    app = Hallpass(
        verifier=verifier,
        vault=vault,
        audit=audit,
        rate_limiter=limiter,
        idempotency=idem,
    )
    for connector in connectors:
        app.add_connector(connector)
    return app


_DEV_SERVICE_CLAIM = "hp_kind"
_DEV_SERVICE_VALUE = "service"


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

    The minter takes ``service=True`` to mint a service (machine-to-machine)
    token, which the app's verifier recognizes as a service principal — enough
    to exercise a ``ProvisioningGuard`` over spawned agents locally:

        team = Team(mint=lambda n, s: token(n, s, service=True),
                    spawner=..., channel="work",
                    guard=ProvisioningGuard(app.verifier))

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
        service_claim=_DEV_SERVICE_CLAIM,
        service_values=frozenset({_DEV_SERVICE_VALUE}),
        connectors=connectors,
    )

    def token(
        subject: str, scopes: Iterable[str] = (), *, service: bool = False
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + 3600,
        }
        if service:
            claims[_DEV_SERVICE_CLAIM] = _DEV_SERVICE_VALUE
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dev"})

    return app, token
