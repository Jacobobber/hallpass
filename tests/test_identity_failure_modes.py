"""Every test names a way a resource server gets fooled by a token, and
proves this one refuses it. Fail-closed is the only behavior under test."""

import base64
import json

import pytest

from hallpass import StaticJwks, TokenVerifier, VerificationError

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def test_valid_token_yields_subject_and_scopes(verifier, keypair):
    principal = verifier.verify(mint(keypair, scope="notes:read repos:write"))
    assert principal.subject == "alice"
    assert principal.scopes == {"notes:read", "repos:write"}


def test_expired_token_rejected(verifier, keypair):
    with pytest.raises(VerificationError):
        verifier.verify(mint(keypair, exp=1))


def test_wrong_audience_rejected(verifier, keypair):
    """A perfectly valid token for a DIFFERENT service must not work here;
    accepting it makes every app in the tenant a confused deputy."""
    with pytest.raises(VerificationError):
        verifier.verify(mint(keypair, aud="https://other-api.example.test"))


def test_wrong_issuer_rejected(verifier, keypair):
    with pytest.raises(VerificationError):
        verifier.verify(mint(keypair, iss="https://evil-idp.example.test"))


def test_signature_from_wrong_key_rejected(verifier, other_keypair):
    """Signed by an attacker's key but claiming the real kid."""
    with pytest.raises(VerificationError):
        verifier.verify(mint(other_keypair, kid="k1"))


def test_alg_none_rejected(verifier, keypair):
    """The classic: strip the signature, claim alg=none."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "kid": "k1"}).encode()
    ).rstrip(b"=")
    body = base64.urlsafe_b64encode(
        json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "alice"}).encode()
    ).rstrip(b"=")
    forged = header + b"." + body + b"."
    with pytest.raises(VerificationError):
        verifier.verify(forged.decode())


def test_symmetric_alg_rejected(verifier):
    """HS256 signed with a guessable secret must be refused outright --
    asymmetric verification only, no algorithm negotiation."""
    import jwt as pyjwt

    token = pyjwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice"},
        "attacker-controlled-secret-thats-long-enough",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(VerificationError):
        verifier.verify(token)


def test_missing_sub_rejected(verifier, keypair):
    with pytest.raises(VerificationError):
        verifier.verify(mint(keypair, sub=None))


def test_missing_kid_rejected(verifier, keypair):
    import jwt as pyjwt
    import time

    token = pyjwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": int(time.time()) + 300},
        keypair,
        algorithm="RS256",
    )
    with pytest.raises(VerificationError):
        verifier.verify(token)


def test_unknown_kid_fails_closed(keypair):
    jwks = StaticJwks({"keys": [jwk_for(keypair, "k1")]})
    v = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks)
    with pytest.raises(VerificationError):
        v.verify(mint(keypair, kid="k-unknown"))


def test_rotated_key_found_via_single_refresh(keypair, other_keypair):
    """Key rotation mid-cache: the verifier refreshes the JWKS exactly once
    for an unknown kid, finds the rotated key, and verifies."""

    class RotatingJwks:
        def __init__(self):
            self.calls = 0
            self.stale = {"keys": [jwk_for(keypair, "k1")]}
            self.fresh = {
                "keys": [jwk_for(keypair, "k1"), jwk_for(other_keypair, "k2")]
            }

        def get(self, *, force_refresh: bool = False):
            self.calls += 1
            return self.fresh if force_refresh else self.stale

    source = RotatingJwks()
    v = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=source)
    principal = v.verify(mint(other_keypair, kid="k2"))
    assert principal.subject == "alice"
    assert source.calls == 2  # one cached read, one forced refresh, no loop


def test_empty_and_garbage_tokens_rejected(verifier):
    for bad in ("", "not-a-jwt", "a.b", "a.b.c.d"):
        with pytest.raises(VerificationError):
            verifier.verify(bad)


def test_error_messages_never_contain_the_token(verifier, keypair):
    """Verification errors get logged; the token must not ride along."""
    token = mint(keypair, exp=1)
    try:
        verifier.verify(token)
    except VerificationError as err:
        assert token not in str(err)
        assert "alice" not in str(err)


def test_scp_list_shape_parses(verifier, keypair):
    principal = verifier.verify(mint(keypair, scope=None, scp=["a:read", "b:write"]))
    assert principal.scopes == {"a:read", "b:write"}


def test_no_scopes_means_no_scopes(verifier, keypair):
    principal = verifier.verify(mint(keypair, scope=None))
    assert principal.scopes == frozenset()


def test_empty_subject_rejected(verifier, keypair):
    """A present-but-blank sub passes JWT's require check but would
    collapse every user onto one partition key downstream."""
    with pytest.raises(VerificationError):
        verifier.verify(mint(keypair, sub=""))
