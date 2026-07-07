"""doctor() is the clone-and-run confidence check: it should catch the things
a newcomer forgets (no connectors, an ephemeral vault, no audit, no rate limit)
without a network call, and stay quiet when the app is properly configured.

The failures these prevent: shipping a server that serves nothing, or one that
silently drops every user's credential on the next restart, with no signal."""

from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    Finding,
    Hallpass,
    InMemoryAuditLog,
    StaticJwks,
    TokenVerifier,
    ToolKit,
    build,
    catalog,
    doctor,
    format_report,
)

from conftest import AUDIENCE, ISSUER, jwk_for


class FakeHttp:
    def request(self, method, url, *, headers, params, json):
        return {}


def _verifier(keypair):
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )


def _notes_kit():
    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:read"], description="read a note")
    def read_note(ctx, id: str):
        return {"id": id}

    return kit


def _codes(app):
    return {f.code: f.level for f in doctor(app)}


def test_empty_app_is_an_error(keypair):
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=_verifier(keypair), vault=vault)
    codes = _codes(app)
    assert codes["no-tools"] == "error"
    # the error sorts to the top of the report
    assert doctor(app)[0].code == "no-tools"
    vault.close()


def test_dev_style_app_warns_about_the_soft_spots(keypair):
    # one connector, but in-memory vault and no audit / rate limit
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=_verifier(keypair), vault=vault)
    app.add_connector(catalog.load("github", http=FakeHttp()))

    codes = _codes(app)
    assert codes["tools"] == "ok"
    assert codes["no-audit"] == "warn"
    assert codes["no-rate-limit"] == "warn"
    assert codes["ephemeral-vault"] == "warn"
    assert "no-tools" not in codes
    vault.close()


def test_fully_configured_app_is_all_clear(tmp_path):
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": []}),  # doctor never verifies a token
        vault_key=Fernet.generate_key(),
        vault_path=str(tmp_path / "vault.db"),  # durable
        audit=InMemoryAuditLog(),
        rate_limit=(10, 60.0),
        connectors=[_notes_kit()],
    )
    findings = doctor(app)
    assert all(f.level == "ok" for f in findings), format_report(findings)
    app.close()


def test_unavailable_connector_is_flagged(tmp_path):
    class Down:
        service = "down"

        def tools(self):
            return []

        def available(self):
            return False

    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": []}),
        vault_key=Fernet.generate_key(),
        vault_path=str(tmp_path / "v.db"),
        audit=InMemoryAuditLog(),
        rate_limit=(10, 60.0),
        connectors=[_notes_kit(), Down()],
    )
    codes = _codes(app)
    assert codes["unavailable-connectors"] == "warn"
    app.close()


def test_format_report_renders_each_finding():
    text = format_report(
        [Finding("error", "no-tools", "nothing"), Finding("ok", "tools", "1 tool")]
    )
    assert "no-tools" in text and "tools" in text
    assert "ERR" in text and "OK" in text
