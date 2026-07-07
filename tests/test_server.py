"""The batteries-included setup: build() wires a real app from minimal
config, and dev_app() gives a zero-config, fully-gated server plus a token
minter. The point is that easy setup does not weaken any guarantee."""

import pytest

from hallpass import (
    Hallpass,
    RateLimited,
    StaticJwks,
    ToolKit,
    build,
    dev_app,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def a_kit():
    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:read"])
    def read_note(ctx, id: str):
        "Read a note."
        return f"note {id}"

    return kit


def test_build_requires_a_jwks_source():
    with pytest.raises(ValueError):
        build(issuer=ISSUER, audience=AUDIENCE)


def test_build_wires_a_working_app(keypair):
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        connectors=[a_kit()],
    )
    assert isinstance(app, Hallpass)
    token = mint(keypair, sub="alice", scope="notes:read")
    assert app.call_tool(token, "read_note", {"id": "1"}) == "note 1"
    app.close()


def test_build_wires_rate_limit(keypair):
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        connectors=[a_kit()],
        rate_limit=(1, 60),
    )
    token = mint(keypair, sub="alice", scope="notes:read")
    app.call_tool(token, "read_note", {"id": "1"})
    with pytest.raises(RateLimited):
        app.call_tool(token, "read_note", {"id": "2"})
    app.close()


def test_dev_app_is_a_fully_gated_server():
    kit = a_kit()
    app, token = dev_app(connectors=[kit])
    # authorized call works
    assert (
        app.call_tool(token("alice", ["notes:read"]), "read_note", {"id": "9"})
        == "note 9"
    )
    # gating still enforced through dev tokens
    assert app.list_tools(token("bob", [])) == []
    with pytest.raises(Exception):
        app.call_tool(token("bob", []), "read_note", {"id": "9"})
    app.close()


def test_dev_app_tokens_are_real_and_verified():
    """The minted token is a genuine RS256 token the app verifies; a
    garbage token is still refused, so dev mode does not bypass auth."""
    app, token = dev_app(connectors=[a_kit()])
    with pytest.raises(Exception):
        app.list_tools("not-a-real-token")
    assert isinstance(token("alice", ["notes:read"]), str)
    app.close()


def test_build_generates_a_vault_key_when_omitted(keypair):
    # No vault_key: build generates one; the app still works within the process.
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    assert isinstance(app, Hallpass)
    app.close()
