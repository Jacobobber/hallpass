"""An unconfigured connector must not advertise tools it cannot serve.
When a connector reports unavailable at registration, its tools are
absent from every catalog and uncallable, and the skip is observable."""

import pytest
from cryptography.fernet import Fernet

from hallpass import CredentialVault, Hallpass, StaticJwks, TokenVerifier, ToolSpec

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def _spec(name, service):
    return ToolSpec(
        name=name,
        description=name,
        required_scopes=frozenset(),
        handler=lambda ctx, **kw: "ok",
        connector=service,
    )


class ConfiguredNotes:
    service = "notes"

    def available(self):
        return True

    def tools(self):
        return [_spec("read_note", "notes")]


class UnconfiguredCrm:
    service = "crm"

    def __init__(self):
        self.tools_called = False

    def available(self):
        return False  # e.g. no API key configured

    def tools(self):
        self.tools_called = True
        return [_spec("read_lead", "crm")]


@pytest.fixture()
def app(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault)
    yield application, vault
    vault.close()


def test_unavailable_connector_tools_are_absent_and_uncallable(app, keypair):
    application, _ = app
    application.add_connector(ConfiguredNotes())
    crm = UnconfiguredCrm()
    application.add_connector(crm)

    token = mint(keypair, sub="alice", scope="")
    names = {t.name for t in application.list_tools(token)}
    assert names == {"read_note"}
    assert "read_lead" not in names
    assert not crm.tools_called  # its tools() was never even enumerated

    # And calling it is refused as if it does not exist.
    with pytest.raises(Exception):
        application.call_tool(token, "read_lead", {})


def test_unavailable_is_reported(app):
    application, _ = app
    application.add_connector(ConfiguredNotes())
    application.add_connector(UnconfiguredCrm())
    assert application.unavailable_connectors == ["crm"]


def test_connector_without_available_method_is_treated_available(app, keypair):
    """Back-compat: a connector that never heard of available() still works."""
    application, _ = app

    class Legacy:
        service = "legacy"

        def tools(self):
            return [_spec("ping", "legacy")]

    application.add_connector(Legacy())
    token = mint(keypair, sub="alice", scope="")
    assert "ping" in {t.name for t in application.list_tools(token)}
