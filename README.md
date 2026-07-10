# hallpass

[![CI](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml/badge.svg)](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml)

An auth-native substrate for organizing fleets of independently-credentialed agents: every agent is its own identity, capability is its scope, and the same layer that gates a single tool call also enforces and audits the whole organization.

One idea runs the entire stack. The verify-and-gate that decides whether *this* caller may make *this* tool call is the same decision that governs who an agent may message, who may be spawned with what capability, and who may pull which task from a shared queue. So an "organization" of agents is not an orchestration product bolted on top of an auth library — it *is* the auth layer expressed at fleet scale. Every agent authenticates as its own service identity (its own keys, never a human's session), a capability is exactly a scope set, and who-can-do-what — including who may approve whom — is enforced at call time and recorded in one audit trail.

**Status: v1.25.0 — stable.** The public API (everything exported from `hallpass`) is committed to under semver since 1.0; 504 tests (6 gated on a Postgres), `mypy --strict`, and ruff green on a Linux + Windows × Python 3.10–3.14 matrix. What is here and what is next: [CHANGELOG.md](CHANGELOG.md) and [docs/PLATFORM.md](docs/PLATFORM.md). The design behind it: [docs/multi-user-is-the-hard-part.md](docs/multi-user-is-the-hard-part.md) (the auth core) and [docs/agent-identity-and-organization.md](docs/agent-identity-and-organization.md) (the org at scale).

## Clone and run

`uv` is the only prerequisite. From a fresh clone:

```bash
git clone https://github.com/Jacobobber/hallpass && cd hallpass
uv run python examples/quickstart.py     # a gated, per-user tool server, no setup
uv run hallpass serve --dev              # a live HTTP server + a token + curl to hit it
uv run --group dev pytest -q             # the full suite
```

That runs a working server with no identity provider and no config. The map of the whole system is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the connector list is [docs/CATALOG.md](docs/CATALOG.md); where it is heading is [docs/PLATFORM.md](docs/PLATFORM.md).

## Quick start

Define a connector by decorating functions, then get a fully gated server with no identity provider to stand up — about ten lines to a working, per-user, scope-gated tool server:

```python
from hallpass import ToolKit, dev_app

kit = ToolKit("notes")

@kit.tool(scopes=["notes:read"])
def read_note(ctx, id: str):
    "Read the caller's note by id."          # description comes from here
    return f"note {id} for {ctx.principal.subject}"

app, token = dev_app(connectors=[kit])       # zero-config dev server + a token minter
print(app.call_tool(token("alice", ["notes:read"]), "read_note", {"id": "7"}))
# -> note 7 for alice
print(app.list_tools(token("bob", [])))       # bob lacks the scope: []
```

The tool's name comes from the function, its description from the docstring, its argument schema from the signature. `dev_app` is for local development; `build(...)` wires the same server against your real OIDC provider and a persistent vault key:

```python
from hallpass import build

app = build(
    issuer="https://your-idp.example.com",
    audience="https://your-mcp-server.example.com",
    jwks_url="https://your-idp.example.com/.well-known/jwks.json",
    vault_key=os.environ["HALLPASS_VAULT_KEY"],   # persist this to survive restarts
    connectors=[kit],
    rate_limit=(60, 60.0),                        # optional: 60 calls / 60s per user
)
```

## The one idea: identity → capability → gate

Three layers answer the three questions a multi-user server cannot dodge — who is this request from, where do each user's downstream credentials live, and which tools does *this* caller get. Full treatment in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); in brief:

- **Identity** (`TokenVerifier`) — OAuth 2.1 resource-server verification: RS256 against a JWKS, exact issuer/audience, `alg=none` and symmetric algorithms refused, fail-closed on an unknown key. A `Principal` is a user or a service (machine-to-machine); the distinction is descriptive, access stays scope-decided.
- **Vault** (`CredentialVault`) — downstream credentials encrypted at rest (Fernet), keyed by `(subject, service)`. A handler can only reach *its own* principal's credential for *its own* service; cross-subject access is unrepresentable at the seam, not merely forbidden.
- **Gate** (`ToolGate`) — connectors declare required scopes per tool; the check runs at call time, not just in the catalog. Deny is the default everywhere, and an ungranted tool is indistinguishable from one that does not exist.

## From one call to an organization

The same verify-and-gate composes outward. Nothing below bypasses the core; each layer is the same identity and scope model pointed at a new resource. Depth (FLEX grammar, presence, DM derivation, lease semantics, the reference loop) is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/agent-identity-and-organization.md](docs/agent-identity-and-organization.md).

- **Agent-to-agent channels** (`A2ABus`) — authenticated, authorized, durable channels (append-only log, forward-only ack, catch-up on reconnect). A `ChannelPolicy` gates post/read by scope; denial is opaque; bodies are sanitized on read (control/escape/bidi/zero-width) with an injection-resistant framing helper.
- **FLEX** — a token-efficient message language: `<kind> [@to]* [#ref]* [k=v]* [ | note]`, ~44% smaller than compact JSON for a representative message, round-trips, tolerant parse, sanitized in.
- **Presence & DM** — `announce`/`roster` show who is live on a channel now (soft state, never a grant); `open_dm` derives a private 1:1 channel gated by a scope only the two parties hold (privacy *is* the scope gate).
- **Orchestrate, route, queue** — `Orchestrator`/`Worker` dispatch tasks and gather results as FLEX messages over a channel; `Router` picks a worker by capability (the scopes that gate its tools decide what work it can take); `TaskQueue` is a durable, lease-based backlog (exactly-once claim, dead-worker recovery, idempotent complete).
- **Spawn scoped agents** — the step past dispatching to workers that exist is *creating* them:

```python
from hallpass import Team, SubprocessSpawner, AgentSpec, dev_app

_, mint = dev_app()   # in production: ClientCredentialsMinter (one IdP client per agent)
team = Team(mint=lambda name, scopes: mint(name, scopes),
            spawner=SubprocessSpawner(["python", "agent.py"]), channel="work")

team.spawn(AgentSpec("reviewer",  scopes=frozenset({"github:read"}),  task="review PR 42"))
team.spawn(AgentSpec("messenger", scopes=frozenset({"slack:write"}), task="post to #eng"))
```

**Each spawned agent is a scoped identity, not a trusted one.** It is minted a token carrying only its harness scopes, and it reaches only the credentials vaulted under *its own* subject — so a spawned agent cannot act with the human operator's identity, and a compromised or confused agent can only touch what its harness grants, enforced at call time and audited. Give the `Team` a `ProvisioningGuard` and that becomes a checked invariant: a minted token that isn't the agent's own scoped *service* identity (subject == name, exactly the harness scopes) raises `ProvisioningError` *before* the process launches. A `HarnessRegistry` goes one level up — declare a harness type's maximum scopes once (`Harness("reviewer", {...})`), and any agent spawned under it is bounded to that preset. Isolation between agents is the auth layer, not a promise. hallpass stays model-agnostic: it provisions the identity, harness, task, and channel and launches the process; the loop inside is yours (`serve_queue`/`run_worker` are reusable, model-free loop shells). Why this matters — API-key-per-agent instead of a shared human harness, and how it scales into a governed org — is [docs/agent-identity-and-organization.md](docs/agent-identity-and-organization.md). Demos: [`examples/orchestrator.py`](examples/orchestrator.py), [`examples/spawn_agents.py`](examples/spawn_agents.py), [`examples/reference_agent.py`](examples/reference_agent.py).

## Prewired connectors

hallpass ships a catalog of connectors to real services, so you don't write one to get started. A connector is a declaration, not code — a base URL, an auth style, and a list of endpoints, each becoming a gated tool that calls the service's REST API with the caller's vaulted credential — which is what makes the catalog cheap to grow. Today: **47 services / 115 tools, 22 with a prewired OAuth flow** (the full list, with auth styles, form/GraphQL/multi-credential mechanics, and per-tenant notes, is [docs/CATALOG.md](docs/CATALOG.md)).

```python
from hallpass import catalog, dev_app

app, token = dev_app(connectors=catalog.load_all())     # every non-per-tenant connector
app._vault.store("alice", "github", "ghp_...")          # the user's connected token
app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
```

For the 22 OAuth services, `OAuthConnect` drives the connect flow end to end (single-use state + PKCE, code exchange, self-healing refresh on 401/403) and records a listable, revocable consent grant:

```python
from hallpass import OAuthConnect, catalog

connect = OAuthConnect(vault=app._vault, providers={"github": catalog.oauth_provider(
    "github", client_id=CID, client_secret=SECRET, redirect_uri="https://your-app/callback")})
url = connect.start("alice", "github")   # redirect the user here; then:
connect.finish(state, code)              # tokens stored where the connector reads them
```

Requires the `connectors` extra for the default httpx client; inject any `HttpClient` to use your own. `SqlitePendingStore` shares OAuth state across instances; `SqliteConsentLedger` persists grants.

## Run it (CLI · HTTP · MCP)

Installing hallpass puts a `hallpass` command on your path — a thin shell over the library, so it can't drift from it:

```bash
hallpass serve --dev            # a live multi-user server on localhost + a demo token + curl
hallpass doctor --dev           # config self-check (exits non-zero on an error)
hallpass catalog list           # every connector and its tool count
```

`hallpass serve --dev` stands up a real stdlib-only HTTP server and prints a token plus the curl to hit it; endpoints route straight through the core (`/tools` is the caller's gated catalog, `/call/<tool>` verifies and gates, an ungranted tool is the same `404` as a nonexistent one). It's a reference/dev transport — put TLS and rate limiting at a proxy. For MCP, `hallpass[mcp]` adds a thin adapter (`build_mcp_server(app, token_provider)`) that gates every list and call against the caller's bearer and advertises read-only/destructive/idempotent hints. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The operational layer

Access is decided by the three layers above; these keep it honest and safe once it has users. All are optional and off by default:

| Concern | Primitive | What it does |
|---|---|---|
| Audit | `SqliteAuditLog` / `AuditSink` | records every list and call, allows *and* denials, with `duration_ms` (the trail doubles as a latency source); never a token or credential |
| Rate limiting | `FixedWindowRateLimiter` | per-subject call budgets so one looping agent can't hammer a downstream for everyone |
| Availability | `Connector.available()` | an unconfigured connector never advertises tools it can't serve |
| Retry | `RetryingHttpClient` | 429/5xx with backoff honoring `Retry-After`; 401/403 left to auth-refresh |
| Circuit breaker | `CircuitBreakerHttpClient` | opens after a run of outages, fails fast, half-open probe recovers |
| Response guard | `guard_response` | an oversized result becomes an explicit truncation envelope, never a silent flood |
| Idempotency | `IdempotencyStore` | a retried mutation with the same key runs at most once |

## Security posture

Every test names a way multi-user tool servers get broken and proves this design refuses it: wrong-audience tokens (the confused deputy), `alg=none` and HS256 downgrade, wrong-key signatures, unknown-kid fail-closed with single-refresh rotation, secrets in the database file, cross-user and cross-service vault isolation, call-time gating bypass, partial-scope unlock, and cross-layer leaks. Beyond the named cases, `tests/test_properties.py` puts the core invariants under a Hypothesis fuzzer and prints the exact input that breaks one. Scope and threat model: [SECURITY.md](SECURITY.md).

## Performance

hallpass stays out of the way on the hot path: verified principals are cached until the token's own expiry (a repeated bearer skips the RSA check), and the default HTTP client pools connections. Measured with `python evals/benchmark.py`:

| operation | ops/sec |
|---|---|
| token verify, cached | ~3,500,000 |
| token verify, uncached (RSA) | ~22,000 |
| call-time gating | ~4,700,000 |
| FLEX encode / parse | ~630,000 / ~285,000 |

A repeated token is ~160× cheaper than a cold verify; pooled HTTP is ~5× unpooled even on loopback. Numbers are on the author's machine, for relative comparison. Async/uvloop is a deliberate non-goal (see [docs/PLATFORM.md](docs/PLATFORM.md)).

## What this is not

Not an identity provider (bring any OIDC issuer), not an MCP framework (the core is transport-agnostic; the MCP adapter is a thin optional extra), and not a gateway in front of servers — it is the inside of one server done right. It is also **not (yet) a deployed platform**: it is the substrate you would build a multi-agent org platform *on*, and it deliberately runs no model loop — the thinking inside an agent is yours. Where the platform goes from here: [docs/PLATFORM.md](docs/PLATFORM.md).

## Install

```bash
pip install git+https://github.com/Jacobobber/hallpass                       # core
pip install "hallpass[mcp] @ git+https://github.com/Jacobobber/hallpass"     # + MCP adapter
```

Python 3.10+. Two runtime dependencies for the core (`pyjwt[crypto]`, `cryptography`); the MCP adapter and the httpx client/JWKS fetcher are optional extras. CI covers 3.10–3.14 on Linux and Windows with ruff and `mypy --strict`.

## Contributing

Issues and questions welcome. This is a reference implementation of an auth boundary, so the bar is high: anything touching the verifier, vault, gate, or the coordination layers needs a test naming the property it protects. See [CONTRIBUTING.md](CONTRIBUTING.md); security reports in [SECURITY.md](SECURITY.md).

## License

MIT
