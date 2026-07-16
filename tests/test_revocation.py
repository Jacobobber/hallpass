"""A JWT is valid until its own exp, so without a revocation check 'revoke a
compromised agent' is theater -- the token keeps working. These pin that a
revoked subject stops verifying on the very next call, that the verified-token
cache cannot bypass it, that restore lifts it, and that with no revocation list
the behavior is unchanged."""

import pytest

from hallpass import (
    InMemoryRevocationList,
    StaticJwks,
    TokenVerifier,
    VerificationError,
    build,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def _verifier(keypair, revocations=None):
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        revocations=revocations,
    )


def test_revoke_stops_a_live_token_immediately(keypair):
    rev = InMemoryRevocationList()
    verifier = _verifier(keypair, rev)
    token = mint(keypair, sub="agent-7", scope="build:write")
    # verifies (and caches) before revocation
    assert verifier.verify(token).subject == "agent-7"
    rev.revoke("agent-7", reason="compromised")
    # the SAME token -- cached and not yet expired -- is now refused: the cache
    # must not bypass a revoke issued after it was cached.
    with pytest.raises(VerificationError):
        verifier.verify(token)


def test_restore_lifts_the_revocation(keypair):
    rev = InMemoryRevocationList()
    verifier = _verifier(keypair, rev)
    token = mint(keypair, sub="agent-7", scope="x")
    rev.revoke("agent-7")
    with pytest.raises(VerificationError):
        verifier.verify(token)
    rev.restore("agent-7")
    assert verifier.verify(token).subject == "agent-7"


def test_revocation_is_per_subject(keypair):
    rev = InMemoryRevocationList()
    verifier = _verifier(keypair, rev)
    rev.revoke("agent-7")
    # a different subject is unaffected
    other = mint(keypair, sub="agent-8", scope="x")
    assert verifier.verify(other).subject == "agent-8"
    assert rev.revoked() == ["agent-7"]


def test_no_revocation_list_is_unchanged(keypair):
    verifier = _verifier(keypair, None)  # no revocations wired
    token = mint(keypair, sub="agent-7", scope="x")
    assert verifier.verify(token).subject == "agent-7"


def test_build_wires_revocations(keypair):
    rev = InMemoryRevocationList()
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        revocations=rev,
    )
    token = mint(keypair, sub="agent-7", scope="x")
    assert app.principal(token).subject == "agent-7"
    rev.revoke("agent-7")
    with pytest.raises(VerificationError):
        app.principal(token)
    app.close()
