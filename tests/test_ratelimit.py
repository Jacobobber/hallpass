"""Rate limiting protects the tools behind the bridge from one agent in a
loop. The properties that matter: the budget is enforced, it is per
subject (one caller's burst never starves another), it resets as the
window slides, and an over-budget call is refused and audited."""

import time

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    FixedWindowRateLimiter,
    Hallpass,
    InMemoryAuditLog,
    RateLimited,
    StaticJwks,
    TokenVerifier,
    ToolSpec,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def test_limiter_blocks_over_budget():
    limiter = FixedWindowRateLimiter(max_calls=3, window_seconds=60)
    for _ in range(3):
        limiter.check("alice")
    with pytest.raises(RateLimited):
        limiter.check("alice")


def test_limiter_is_per_subject():
    limiter = FixedWindowRateLimiter(max_calls=1, window_seconds=60)
    limiter.check("alice")
    limiter.check("bob")  # bob has his own budget
    with pytest.raises(RateLimited):
        limiter.check("alice")


def test_window_slides():
    limiter = FixedWindowRateLimiter(max_calls=1, window_seconds=0.2)
    limiter.check("alice")
    with pytest.raises(RateLimited):
        limiter.check("alice")
    time.sleep(0.25)
    limiter.check("alice")  # window passed, budget restored


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(max_calls=0, window_seconds=60)
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(max_calls=1, window_seconds=0)


class NotesConnector:
    service = "notes"

    def tools(self):
        return [
            ToolSpec(
                name="read_note",
                description="read",
                required_scopes=frozenset({"notes:read"}),
                handler=lambda ctx, **kw: "ok",
            )
        ]


def test_call_tool_enforces_and_audits_the_limit(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    log = InMemoryAuditLog()
    app = Hallpass(
        verifier=verifier,
        vault=vault,
        audit=log,
        rate_limiter=FixedWindowRateLimiter(max_calls=2, window_seconds=60),
    )
    app.add_connector(NotesConnector())
    token = mint(keypair, sub="alice", scope="notes:read")

    assert app.call_tool(token, "read_note", {}) == "ok"
    assert app.call_tool(token, "read_note", {}) == "ok"
    with pytest.raises(RateLimited):
        app.call_tool(token, "read_note", {})

    denies = [e for e in log.events() if e.decision == "deny"]
    assert denies and denies[-1].reason == "rate_limited"
    vault.close()
