"""Property-based auth-isolation evals.

The unit tests check the security invariants at hand-picked points. These push
on them with generated inputs: across many random scope sets, subjects, and
queries, do the core guarantees the README makes actually hold?

Invariants asserted:
  1. Call-time gating: a tool runs iff the caller holds all its required scopes.
  2. Vault isolation: no subject can read another subject's stored credential.
  3. Search subset: tool search never returns a tool the caller cannot call.
  4. A2A read gating: a principal without the read scope can never read a channel.

A failing example prints the exact scope set / subject that broke it.
"""

import json
import string
import time

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hallpass import (
    A2ABus,
    ChannelDenied,
    ChannelPolicy,
    CredentialVault,
    Hallpass,
    Principal,
    ToolKit,
    UnknownTool,
)
from hallpass.identity import StaticJwks, TokenVerifier

ISSUER = "https://idp.example.test"
AUDIENCE = "https://tools.example.test"

# A module-level signing key, so @given tests don't tangle with pytest fixtures
# (hypothesis resolves a fixture once per test, not per example).
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
_JWK.update(kid="k1", use="sig", alg="RS256")
_JWKS = StaticJwks({"keys": [_JWK]})

ALL_SCOPES = ["a:read", "a:write", "b:read", "b:write", "c:read"]
scope_sets = st.sets(st.sampled_from(ALL_SCOPES))
subjects = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6)


def _mint(sub: str, scopes: set[str]) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "scope": " ".join(sorted(scopes)),
            "iat": now,
            "exp": now + 3600,
        },
        _KEY,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


def _verifier() -> TokenVerifier:
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=_JWKS)


def _kit_requiring(scope: str, tool_name: str) -> ToolKit:
    kit = ToolKit(scope.split(":")[0])

    @kit.tool(scopes=[scope], name=tool_name, description="x")
    def run(ctx, **kwargs):
        return {"ok": tool_name}

    return kit


@settings(max_examples=75)
@given(granted=scope_sets)
def test_gating_holds_for_any_scope_set(granted):
    required = "a:read"
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=_verifier(), vault=vault)
    app.add_connector(_kit_requiring(required, "tool_a"))
    token = _mint("u", granted)
    if required in granted:
        assert app.call_tool(token, "tool_a", {}) == {"ok": "tool_a"}
    else:
        with pytest.raises(UnknownTool):  # ToolDenied subclasses UnknownTool
            app.call_tool(token, "tool_a", {})
    vault.close()


@settings(max_examples=100)
@given(owner=subjects, other=subjects, secret=st.text(min_size=1, max_size=30))
def test_vault_isolation_holds_across_subjects(owner, other, secret):
    assume(owner != other)
    vault = CredentialVault(Fernet.generate_key())
    vault.store(owner, "svc", secret)
    assert vault.fetch(owner, "svc") == secret  # owner reads their own
    assert vault.fetch(other, "svc") is None  # nobody else can, ever
    vault.close()


@settings(max_examples=75)
@given(granted=scope_sets, query=st.text(max_size=25))
def test_search_never_exceeds_authorized_set(granted, query):
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=_verifier(), vault=vault)
    # a spread of tools across distinct scopes
    app.add_connector(_kit_requiring("a:read", "ta"))
    app.add_connector(_kit_requiring("b:read", "tb"))
    app.add_connector(_kit_requiring("c:read", "tc"))
    token = _mint("u", granted)
    authorized = {s.name for s in app.list_tools(token)}
    hits = app.search_tools(token, query)
    assert all(h.name in authorized for h in hits)
    vault.close()


@settings(max_examples=75)
@given(granted=scope_sets)
def test_a2a_read_requires_the_read_scope(granted):
    bus = A2ABus()
    bus.declare_channel("ops", ChannelPolicy(read_scopes=frozenset({"c:read"})))
    # seed a message as an authorized poster
    bus.post(Principal("poster", frozenset({"c:read"})), "ops", "hi")
    reader = Principal("reader", frozenset(granted))
    if "c:read" in granted:
        assert [m.body for m in bus.catch_up(reader, "ops")] == ["hi"]
    else:
        with pytest.raises(ChannelDenied):  # opaque by design
            bus.catch_up(reader, "ops")
    bus.close()
