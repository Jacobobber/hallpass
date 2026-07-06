# hallpass

[![CI](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml/badge.svg)](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml)

Multi-user auth core for MCP servers: per-user OAuth 2.1 verification against any OIDC provider, an encrypted per-user credential vault, and scope-derived tool gating that is enforced at call time, not just in the catalog.

**Status: pre-release (v0.1).** The core, the MCP adapter, and the security suite are in place and green; treat the API as unstable until v0.2.

The design essay behind this: [Multi-user is the hard part of an MCP server](docs/multi-user-is-the-hard-part.md).

## The gap

Nearly every public MCP server is single-user: one process, one identity, credentials in env vars. The moment a server fronts real services for a team, three problems appear that the toy pattern cannot answer: who is this request actually from, where do each user's downstream credentials live, and which tools does *this* user get. hallpass is those three answers as a small, transport-agnostic core - a hall pass is a scoped, revocable permission slip issued by an authority and checked by whoever stops you in the corridor, which is exactly what a bearer token is supposed to be.

## The three layers

1. **Identity** (`TokenVerifier`): boring-on-purpose OAuth 2.1 resource-server verification. RS256 against a JWKS, exact issuer and audience, `alg=none` and symmetric algorithms refused, single JWKS refresh on unknown kid then fail closed. The JWKS source is injected: production uses an OIDC provider over HTTPS, tests use a static document, the logic is identical.
2. **Vault** (`CredentialVault`): downstream service credentials encrypted at rest (Fernet), keyed by (user subject, service). Tool handlers receive a context that can only reach the calling user's credential for that connector's service - cross-user access is unrepresentable at the seam, not forbidden by convention.
3. **Gating** (`ToolGate`): connectors declare required scopes per tool. The catalog is per-principal, and the same check runs again at call time - a client that ignores the menu and calls directly is refused. Deny is the default everywhere.

## Quickstart

```python
from cryptography.fernet import Fernet
from hallpass import CredentialVault, Hallpass, HttpJwks, TokenVerifier

verifier = TokenVerifier(
    issuer="https://your-idp.example.com",
    audience="https://your-mcp-server.example.com",
    jwks=HttpJwks("https://your-idp.example.com/.well-known/jwks.json"),
)
vault = CredentialVault(Fernet.generate_key())  # bring your own key management
app = Hallpass(verifier=verifier, vault=vault)
app.add_connector(YourConnector())              # your Connector (see below)

bearer_token = ...                              # the validated bearer from your transport
tools = app.list_tools(bearer_token)            # this user's catalog
result = app.call_tool(bearer_token, "read_note", {})
```

A connector is a class with a `service` name and a `tools()` method returning `ToolSpec`s; handlers get a `UserContext` with the caller's identity and that user's credential for the connector's service. For a complete, runnable `YourConnector` plus a minted token, copy the `NotesConnector` in [`tests/test_end_to_end.py`](tests/test_end_to_end.py).

## Serving it over MCP

The core is transport-agnostic; `hallpass[mcp]` adds a thin adapter that wires it into an MCP low-level server. You supply a token provider that reads the validated bearer from your transport's auth context (the ASGI scope, under streamable HTTP):

```python
from hallpass.mcp_adapter import build_mcp_server

async def token_provider() -> str:
    return current_request_bearer()  # from your transport's auth context

server = build_mcp_server(app, token_provider)  # hand to any MCP transport
```

Every list and call the server answers is gated by the core against that token. An unauthenticated caller gets an empty catalog and a refused call; an ungranted tool is indistinguishable from one that does not exist. See `tests/test_mcp_adapter.py`.

## Install

```bash
pip install git+https://github.com/Jacobobber/hallpass        # core
pip install "hallpass[mcp] @ git+https://github.com/Jacobobber/hallpass"  # + MCP adapter
```

Python 3.10+. One runtime dependency for the core (`pyjwt[crypto]`); the MCP adapter and the HTTP JWKS fetcher are optional extras. CI covers 3.10/3.12/3.14 on Linux and Windows, with ruff and `mypy --strict`.

## The security suite

Every test names a way multi-user tool servers get broken and proves this design refuses it: wrong-audience tokens (the confused deputy), `alg=none` and HS256 downgrade, signatures from the wrong key, unknown-kid fail-closed with single-refresh rotation, secrets in the database file, cross-user and cross-service vault isolation, wrong-key decryption, call-time gating bypass, partial-scope unlock, and cross-layer leaks (user B's call can never surface user A's credential).

```bash
uv run --group dev pytest -q
```

## What this is not

Not an identity provider (bring Keycloak, Auth0, or any OIDC issuer), not an MCP framework (the core is transport-agnostic; the MCP adapter is a thin optional extra), and not a gateway in front of servers - it is the inside of one server done right.

## Contributing

Issues and questions welcome. This is a reference implementation of an auth boundary, so the bar for changes is high: correctness, portability, and security fixes are welcome, and anything touching the verifier, vault, or gate needs a test naming the property it protects. Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT
