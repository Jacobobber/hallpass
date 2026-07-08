"""A verified token may represent a user (delegated) or a service acting as
itself (client-credentials / M2M). When the verifier is told which claim marks
a service, the resulting Principal reports kind="service" / is_service=True;
otherwise every principal is a user. It's descriptive, not a permission —
access is still by scopes."""

from hallpass import Principal
from hallpass.identity import StaticJwks, TokenVerifier, VerificationError

from conftest import AUDIENCE, ISSUER, jwk_for, mint

import pytest


def _verifier(keypair, **kw):
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        **kw,
    )


def test_principal_defaults_to_user():
    p = Principal(subject="alice", scopes=frozenset())
    assert p.kind == "user" and p.is_service is False


def test_service_token_recognized_when_configured(keypair):
    v = _verifier(
        keypair, service_claim="gty", service_values=frozenset({"client-credentials"})
    )
    tok = mint(keypair, sub="svc-agent", scope="build:read", gty="client-credentials")
    p = v.verify(tok)
    assert p.is_service is True and p.kind == "service"
    assert p.subject == "svc-agent"
    assert "build:read" in p.scopes  # scopes work identically


def test_user_token_stays_user_even_when_service_claim_configured(keypair):
    v = _verifier(
        keypair, service_claim="gty", service_values=frozenset({"client-credentials"})
    )
    # a normal user token has no gty claim -> user
    p = v.verify(mint(keypair, sub="alice", scope="build:read"))
    assert p.is_service is False


def test_no_service_config_means_everyone_is_a_user(keypair):
    v = _verifier(keypair)  # service_claim unset
    p = v.verify(mint(keypair, sub="svc", scope="x", gty="client-credentials"))
    assert p.kind == "user"  # the claim is present but the verifier ignores it


def test_service_recognition_does_not_relax_verification(keypair):
    # marking a token as a service must not bypass the normal checks
    v = _verifier(
        keypair, service_claim="gty", service_values=frozenset({"client-credentials"})
    )
    with pytest.raises(VerificationError):
        v.verify(
            mint(keypair, sub="svc", scope="x", gty="client-credentials", aud="wrong")
        )
