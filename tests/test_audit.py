"""The audit trail's job is to leave a trace on every decision. The test
that matters most: a DENIED call is recorded, not just a successful one,
because a refused call is exactly what a security review comes looking
for. And no event ever carries a token, claim value, or credential."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    Hallpass,
    InMemoryAuditLog,
    StaticJwks,
    TokenVerifier,
    ToolSpec,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint

ALICE_SECRET = "alice-secret-key-do-not-log"


class NotesConnector:
    service = "notes"

    def tools(self):
        return [
            ToolSpec(
                name="read_note",
                description="Read the caller's note",
                required_scopes=frozenset({"notes:read"}),
                handler=lambda ctx, **kw: "ok",
            )
        ]


@pytest.fixture()
def app_and_log(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    log = InMemoryAuditLog()
    app = Hallpass(verifier=verifier, vault=vault, audit=log)
    app.add_connector(NotesConnector())
    yield app, log
    vault.close()


def test_allow_is_audited(app_and_log, keypair):
    app, log = app_and_log
    app.call_tool(mint(keypair, sub="alice", scope="notes:read"), "read_note", {})
    events = log.events()
    assert any(
        e.action == "call_tool" and e.decision == "allow" and e.subject == "alice"
        for e in events
    )


def test_denied_call_is_audited(app_and_log, keypair):
    """The lesson: denials must leave a trace too."""
    app, log = app_and_log
    with pytest.raises(Exception):
        app.call_tool(mint(keypair, sub="bob", scope=""), "read_note", {})
    denies = [e for e in log.events() if e.decision == "deny"]
    assert denies, "a denied call left no audit event"
    assert denies[-1].subject == "bob"
    assert denies[-1].tool == "read_note"
    assert denies[-1].reason == "not_authorized"


def test_unauthenticated_call_is_audited_without_a_subject(app_and_log):
    app, log = app_and_log
    with pytest.raises(Exception):
        app.call_tool("garbage-token", "read_note", {})
    denies = [e for e in log.events() if e.decision == "deny"]
    assert denies and denies[-1].reason == "authentication"
    assert denies[-1].subject == "<unverified>"


def test_audit_never_contains_secrets_or_tokens(app_and_log, keypair):
    app, log = app_and_log
    vault_backed = app  # store a credential the tool could read
    token = mint(keypair, sub="alice", scope="notes:read")
    app.call_tool(token, "read_note", {})
    blob = "".join(
        f"{e.subject}|{e.action}|{e.decision}|{e.tool}|{e.reason}" for e in log.events()
    )
    assert token not in blob
    assert ALICE_SECRET not in blob
    del vault_backed


def test_no_sink_is_a_silent_noop(keypair):
    """Audit is optional; with no sink, everything still works."""
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=verifier, vault=vault)  # no audit=
    app.add_connector(NotesConnector())
    assert (
        app.call_tool(mint(keypair, sub="a", scope="notes:read"), "read_note", {})
        == "ok"
    )
    vault.close()
