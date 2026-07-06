import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hallpass import CredentialVault, StaticJwks, TokenVerifier
from cryptography.fernet import Fernet

ISSUER = "https://idp.example.test"
AUDIENCE = "https://tools.example.test"


@pytest.fixture
def anyio_backend():
    # The MCP adapter tests are async; pin anyio to asyncio so the suite
    # needs no trio dependency.
    return "asyncio"


@pytest.fixture(scope="session")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_for(private_key, kid: str) -> dict:
    document = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    document["kid"] = kid
    document["use"] = "sig"
    document["alg"] = "RS256"
    return document


def mint(private_key, *, kid: str = "k1", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "exp": now + 300,
        "iat": now,
        "scope": "notes:read",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def verifier(keypair):
    jwks = StaticJwks({"keys": [jwk_for(keypair, "k1")]})
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks)


@pytest.fixture()
def vault():
    v = CredentialVault(Fernet.generate_key())
    yield v
    v.close()
