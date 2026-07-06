"""Token in, gated tool call out: the whole core wired together with real
signed tokens and an invented Notes connector. The failure modes here are
the cross-layer ones no single unit test can catch."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    Hallpass,
    StaticJwks,
    TokenVerifier,
    ToolDenied,
    ToolSpec,
    VerificationError,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint

ALICE_SECRET = "alice-notes-api-key-do-not-leak"


class NotesConnector:
    service = "notes"

    def tools(self):
        return [
            ToolSpec(
                name="read_note",
                description="Read the caller's note",
                required_scopes=frozenset({"notes:read"}),
                handler=self._read_note,
            ),
            ToolSpec(
                name="connect_notes",
                description="Store the caller's notes credential",
                required_scopes=frozenset({"notes:manage"}),
                handler=self._connect,
            ),
        ]

    def _read_note(self, ctx, **kwargs):
        credential = ctx.credential()
        if credential is None:
            return f"{ctx.principal.subject}: notes not connected"
        return f"{ctx.principal.subject}: note fetched with {credential}"

    def _connect(self, ctx, *, secret: str):
        ctx.store_credential(secret)
        return "connected"


@pytest.fixture()
def app(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault)
    application.add_connector(NotesConnector())
    vault.store("alice", "notes", ALICE_SECRET)
    yield application
    vault.close()


def test_token_to_tool_call(app, keypair):
    token = mint(keypair, sub="alice", scope="notes:read")
    result = app.call_tool(token, "read_note", {})
    assert result == f"alice: note fetched with {ALICE_SECRET}"


def test_user_b_never_sees_user_a_credentials(app, keypair):
    """The seam test: bob's context resolves bob's (absent) credential.
    Alice's secret must be unreachable from bob's call."""
    token = mint(keypair, sub="bob", scope="notes:read")
    result = app.call_tool(token, "read_note", {})
    assert result == "bob: notes not connected"
    assert ALICE_SECRET not in result


def test_unauthenticated_caller_has_no_catalog(app):
    with pytest.raises(VerificationError):
        app.list_tools("garbage-token")
    with pytest.raises(VerificationError):
        app.call_tool("garbage-token", "read_note", {})


def test_call_gated_even_if_client_ignores_catalog(app, keypair):
    """A client that never lists and calls a scoped tool directly is
    refused by the call-time gate, not the menu."""
    token = mint(keypair, sub="bob", scope="")
    with pytest.raises(ToolDenied):
        app.call_tool(token, "read_note", {})


def test_catalog_is_per_principal(app, keypair):
    reader = {t.name for t in app.list_tools(mint(keypair, scope="notes:read"))}
    manager = {t.name for t in app.list_tools(mint(keypair, scope="notes:manage"))}
    assert reader == {"read_note"}
    assert manager == {"connect_notes"}


def test_stored_credential_used_on_next_call(app, keypair):
    manage = mint(keypair, sub="carol", scope="notes:manage")
    read = mint(keypair, sub="carol", scope="notes:read")
    app.call_tool(manage, "connect_notes", {"secret": "carol-key"})
    assert app.call_tool(read, "read_note", {}) == "carol: note fetched with carol-key"


def test_expired_token_refused_even_with_right_scopes(app, keypair):
    token = mint(keypair, sub="alice", scope="notes:read", exp=1)
    with pytest.raises(VerificationError):
        app.call_tool(token, "read_note", {})
