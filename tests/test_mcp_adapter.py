"""The MCP wiring is thin, so the tests only need to prove it does not
LOSE the core's guarantees in translation: the catalog stays per-token,
the call-time gate still refuses, and an unauthenticated caller gets
nothing. Exercised through the low-level Server's own handlers, no
transport and no network."""

import pytest
from cryptography.fernet import Fernet

from hallpass import CredentialVault, Hallpass, StaticJwks, TokenVerifier, ToolSpec
from hallpass.mcp_adapter import build_mcp_server

from conftest import AUDIENCE, ISSUER, jwk_for, mint


NOTE_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
}


class NotesConnector:
    service = "notes"

    def tools(self):
        return [
            ToolSpec(
                name="read_note",
                description="Read the caller's note",
                required_scopes=frozenset({"notes:read"}),
                handler=lambda ctx, **kw: f"note for {ctx.principal.subject}",
                input_schema=NOTE_SCHEMA,
            )
        ]


@pytest.fixture()
def server_with_token(keypair):
    """Returns (server, set_token). set_token controls what the injected
    provider yields, standing in for a transport's auth context."""
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    app = Hallpass(verifier=verifier, vault=vault)
    app.add_connector(NotesConnector())

    holder = {"token": ""}

    async def provider() -> str:
        return holder["token"]

    server = build_mcp_server(app, provider)
    yield server, holder
    vault.close()


async def _list(server):
    handler = server.request_handlers
    from mcp.types import ListToolsRequest

    req = ListToolsRequest(method="tools/list")
    result = await handler[ListToolsRequest](req)
    return result.root.tools


async def _call(server, name, arguments):
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    return await handler(req)


@pytest.mark.anyio
async def test_catalog_is_per_token(server_with_token, keypair):
    server, holder = server_with_token
    holder["token"] = mint(keypair, scope="notes:read")
    tools = await _list(server)
    assert [t.name for t in tools] == ["read_note"]


@pytest.mark.anyio
async def test_declared_input_schema_is_advertised(server_with_token, keypair):
    """A tool that declares an argument schema advertises it over MCP, so
    clients get validation instead of the open-object placeholder."""
    server, holder = server_with_token
    holder["token"] = mint(keypair, scope="notes:read")
    tool = (await _list(server))[0]
    assert tool.inputSchema == NOTE_SCHEMA


@pytest.mark.anyio
async def test_unauthenticated_list_is_empty_not_error(server_with_token):
    server, holder = server_with_token
    holder["token"] = "garbage"
    assert await _list(server) == []


@pytest.mark.anyio
async def test_ungranted_scope_hides_tool(server_with_token, keypair):
    server, holder = server_with_token
    holder["token"] = mint(keypair, scope="")
    assert await _list(server) == []


@pytest.mark.anyio
async def test_call_succeeds_with_scope(server_with_token, keypair):
    server, holder = server_with_token
    holder["token"] = mint(keypair, sub="alice", scope="notes:read")
    result = await _call(server, "read_note", {"id": "n1"})
    assert result.root.content[0].text == "note for alice"


@pytest.mark.anyio
async def test_declared_schema_is_enforced_on_calls(server_with_token, keypair):
    """Advertising the schema is not cosmetic: a call missing a required
    argument is rejected by validation, not passed to the handler."""
    server, holder = server_with_token
    holder["token"] = mint(keypair, sub="alice", scope="notes:read")
    result = await _call(server, "read_note", {})  # missing required 'id'
    assert result.root.isError
    assert "id" in result.root.content[0].text


@pytest.mark.anyio
async def test_call_time_gate_refuses_even_if_listing_skipped(
    server_with_token, keypair
):
    """A client that never lists and calls a scoped tool directly is
    refused by the adapter, because the core refuses it. The refusal must
    NOT name the required scope: that would leak which scope guards a tool
    the caller is not allowed to know exists."""
    server, holder = server_with_token
    holder["token"] = mint(keypair, sub="bob", scope="")
    result = await _call(server, "read_note", {})
    assert result.root.isError
    assert "notes:read" not in result.root.content[0].text


@pytest.mark.anyio
async def test_unauthenticated_call_refused(server_with_token):
    server, holder = server_with_token
    holder["token"] = "garbage"
    result = await _call(server, "read_note", {})
    assert result.root.isError
    assert "authentication required" in result.root.content[0].text


@pytest.mark.anyio
async def test_unknown_and_ungranted_are_indistinguishable(server_with_token, keypair):
    """The response to a probed name must not disclose whether the tool
    exists: an existing-but-ungranted tool returns exactly the unknown-tool
    template for its own name, with no scope named."""
    server, holder = server_with_token
    holder["token"] = mint(keypair, scope="")
    ungranted = await _call(server, "read_note", {})  # exists, no scope
    unknown = await _call(server, "does_not_exist", {})  # truly absent
    assert ungranted.root.isError and unknown.root.isError
    assert ungranted.root.content[0].text == "no tool named 'read_note'"
    assert unknown.root.content[0].text == "no tool named 'does_not_exist'"
    assert "notes:read" not in ungranted.root.content[0].text
