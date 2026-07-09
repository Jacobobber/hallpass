# Architecture

hallpass is one idea — *verify the caller, then gate the action by scope* — applied at every level,
from a single tool call up to an organization of agents. This document is the map: each layer, the
invariant it holds (stated as the failure it refuses, the way the tests are), and the module it lives
in. Nothing above the core bypasses it; each higher layer is the same identity-and-scope model pointed
at a new resource.

```
                       ORGANIZATION  (governance, roles, delegation — see PLATFORM.md; design only)
                            │
  coordination   ┌──────────┴───────────────────────────────────────────────┐
                 │  spawning (agents.py) · orchestrator/router (orchestrator) │
                 │  task queue (taskqueue) · reference loops (runner)         │
                 │  A2A channels + presence + DM (a2a, dm) · FLEX (flex)      │
                 └──────────┬───────────────────────────────────────────────┘
                            │  rides the same verify + scope-gate + audit
  access           ┌────────┴─────────┐
                   │  Gate (gating)   │   which tools may this caller run
                   │  Vault (vault)   │   where each subject's secrets live
                   │  Identity (id.)  │   who is this caller
                   └────────┬─────────┘
  transport / tools    connectors + REST catalog (connectors, rest, catalog, toolkit)
                       MCP adapter · HTTP reference server · CLI (mcp_adapter, http_server, cli)
  operations           audit · rate limit · availability · retry · breaker · guard · idempotency
```

Public surface: everything in `hallpass.__all__` (88 names at 1.10.0) is committed to under semver.

---

## Access core

### Identity — `identity.py`
`TokenVerifier` is OAuth 2.1 resource-server verification, boring on purpose: RS256 against a JWKS,
exact issuer and audience, `alg=none` and symmetric algorithms refused, a single JWKS refresh on an
unknown key then fail closed. The `JwksSource` is injected — `HttpJwks` (HTTPS, TTL-cached) in
production, `StaticJwks` in tests — so the logic is identical either way. A verified `Principal` is a
`(subject, scopes)` pair with a `kind` (`user` or `service`, from a configurable claim such as Auth0's
`gty=client-credentials` or Azure's `idtyp=app`); `is_service` is descriptive, never a permission.
Verified principals are cached until the token's own `exp`, so a repeated bearer skips the RSA check.

*Refuses:* the confused deputy (wrong audience), algorithm downgrade (`alg=none`, HS256-with-public-key),
wrong-key signatures, and unknown-kid replay — each with a named test in `tests/test_identity_failure_modes.py`.

### Vault — `vault.py`
`CredentialVault` stores downstream service credentials encrypted at rest (Fernet, AES-128-CBC + HMAC),
keyed by `(subject, service)`. The Fernet key is operator-supplied (env, KMS, file); the vault never
generates or persists it. There is `fetch(subject, service)` and no cross-subject accessor, so the
`(subject, service)` primary key *is* the isolation boundary: one subject cannot read another's
credential even by a bug. No secret appears in a repr, a log, or an exception.

*Refuses:* cross-user and cross-service credential reads, secrets on disk in the clear, wrong-key
decryption leaking anything but a clean `VaultError`.

### Gate — `gating.py`
Connectors declare the scopes each tool requires (`ToolSpec.scopes`). `ToolGate` builds the catalog
per-principal *and* re-checks at call time, so a client that ignores the menu and calls a tool directly
is refused. Deny is the default; an ungranted tool raises the same opaque error as a nonexistent one
(`ToolDenied` is a subclass of `UnknownTool` with a byte-identical message), so a caller cannot
enumerate what it may not touch. `ToolAnnotations` (read-only / destructive / idempotent, auto-derived
from the HTTP verb for catalog connectors) are advisory hints; access stays scope-decided.

The three compose in `core.py` (`Hallpass.list_tools` / `call_tool` / `search_tools`): verify the
bearer → resolve the principal → gate → run the handler with a `UserContext` that reaches only that
principal's credential for that connector's service (`connectors.py`).

---

## Tools & transport

### Connectors and the REST catalog — `connectors.py`, `rest.py`, `catalog.py`, `toolkit.py`
A `Connector` names a `service` (the vault slot) and returns `ToolSpec`s. A `ToolKit` builds one from
decorated functions; a `RestService` builds one declaratively — a base URL, an auth style
(bearer / token / bot / basic / header / query / templated / multi-credential), and a list of endpoints,
each becoming a gated tool that calls the real API with the caller's vaulted credential. Form-encoded
bodies and first-class GraphQL operations are supported. The prewired catalog is 47 services / 115
tools (see [CATALOG.md](CATALOG.md)); adding one is a ~10-line entry. `search_tools` ranks the caller's
authorized tools (BM25 `LexicalRanker` by default, any `ToolRanker` injectable) — gating runs first, so
search can never surface a tool the caller couldn't call.

### OAuth connect flow — `oauth.py`, `consent.py`
`OAuthConnect` drives the authorization-code connect end to end: `start` returns the authorize URL
(single-use state + PKCE), `finish` exchanges the code and stores the tokens where the connector reads
them, `refresh`/`attach_refresh`/`valid_token` self-heal a stale token on 401/403. `SqlitePendingStore`
shares the pending state across instances. A `ConsentLedger` records the fact of each grant
(`InMemoryConsentLedger` locked for the threaded server; `SqliteConsentLedger` durable);
`disconnect` clears the token, the refresh bundle, and the record.

### Transports — `mcp_adapter.py`, `http_server.py`, `cli.py`
The core is transport-agnostic. `hallpass[mcp]` wires it into an MCP low-level server via
`build_mcp_server(app, token_provider)` — every list and call gated against the caller's bearer, tool
annotations advertised. `http_server.py` is a dependency-free stdlib reference server (pure
`handle_request` + `http.server`), security-reviewed: opaque 404, capped body, no credential/traceback
leakage. The `hallpass` CLI (`serve` / `doctor` / `catalog`) is a thin shell over the library.

---

## Coordination

The same verify + scope-gate + audit, pointed at agents talking to each other instead of tools.

### A2A channels — `a2a.py`
`A2ABus` is authenticated, authorized, durable channels: an append-only per-channel log, a forward-only
ack cursor per `(subject, channel)`, catch-up on reconnect (a read without an ack means redelivery,
never loss). A `ChannelPolicy` sets the scopes to post and to read; denial is opaque, exactly like the
tool gate. Bodies are sanitized on read by default (terminal escapes, control chars, Unicode bidi
overrides, zero-width/invisible characters); `frame_untrusted` wraps a body in an injection-resistant
boundary before it reaches a model. hallpass neutralizes spoofing and hiding; it does not claim to
detect semantic prompt injection.

**Presence** (`announce`/`roster`) is soft liveness state gated by the channel's scopes — a subject that
stops heartbeating ages off, and a roster seat is never a grant. **Direct messages** (`open_dm` /
`direct_channel`) derive an order-independent channel whose policy is a single pair scope only the two
parties hold, so a DM's privacy is the same scope gate as everything else.

### FLEX — `flex.py`
A token-efficient A2A message language: `<kind> [@recipient]* [#ref]* [key=value]* [ | note]`. ~44%
smaller than compact JSON for a representative message; `parse(encode(m)) == m` round-trips; parse is
tolerant (unknown tokens fall into the note) and runs the sanitizer first, since inbound is untrusted.

### Orchestration, routing, queue — `orchestrator.py`, `taskqueue.py`, `runner.py`
`Orchestrator.dispatch`/`gather` + `Worker` drive worker agents over a channel: a task is a FLEX message
addressed to a worker and tagged with an id; the result comes back tagged the same. It rides `A2ABus`,
so it is scope-gated, durable, and audited; delivery is at-least-once (gather de-dups by id) and a
raising handler reports only the exception type. `Router` picks a worker by **capability = scope set**
— the same scopes that gate its tools decide what work it can take, so work never lands on an agent that
couldn't do it, and an unroutable task surfaces as `None`. `TaskQueue` is a durable lease-based backlog:
`claim` hands one worker exactly one task under a write-locked transaction (no double-claim), an expired
lease reclaims an abandoned task (dead-worker recovery), `complete` is idempotent by id. `run_worker`
and `serve_queue` are the reusable, model-agnostic loop shells (claim/receive → handle → report →
heartbeat → stop); the handler body is where a model call would go — hallpass supplies the loop, not the
model.

### Spawning — `agents.py`
`Team.spawn(AgentSpec)` mints each agent a token carrying only its harness scopes and launches it
through a pluggable `Spawner` (default `SubprocessSpawner`), passing name/token/task/channel by
environment; `AgentContext.from_env()` picks them up inside the process. Each spawned agent is a scoped
identity, not a trusted one — the harness (its scope set) is the capability boundary, enforced at call
time and audited. The identity/credential model this implies (own keys per agent, never a human's
harness) and how it scales into a governed organization are in
[agent-identity-and-organization.md](agent-identity-and-organization.md).

---

## Operations — `audit.py`, `ratelimit.py`, `idempotency.py`, `rest.py`, `guard.py`, `diagnostics.py`
All optional, off by default, drawn from running a real bridge: `SqliteAuditLog` (durable, queryable,
records allows *and* denials, with `duration_ms` as a built-in latency source), `FixedWindowRateLimiter`
(per-subject budgets), connector `available()`, `RetryingHttpClient` (429/5xx backoff honoring
`Retry-After`), `CircuitBreakerHttpClient` (backpressure), `guard_response` (explicit truncation
envelope, never a silent flood), `IdempotencyStore` (at-most-once retries), and `doctor()` (a
no-network config self-check).

---

## Storage & scale
Every durable store follows one pattern — a SQLite connection, WAL, a single lock, indexed hot queries:
`CredentialVault`, `A2ABus` (messages/cursors/presence), `TaskQueue`, `SqliteAuditLog`,
`SqlitePendingStore`, `SqliteConsentLedger`. Several cross-cutting stores are already pluggable Protocols
(`AuditSink`, `RateLimiter`, `IdempotencyStore`, `PendingStore`, `ConsentLedger`, `JwksSource`,
`HttpClient`). The stateless request path (verify → gate → vault-read → connector call) scales
horizontally as-is; the SQLite coordination substrate is single-node and needs networked backends for
multi-replica. The measured concurrency and the exact path to multi-replica are in
[PLATFORM.md](PLATFORM.md).

## Assembling it by hand
`build()` and `ToolKit` are conveniences over the same core; wired by hand it is:

```python
from cryptography.fernet import Fernet
from hallpass import CredentialVault, Hallpass, HttpJwks, TokenVerifier

verifier = TokenVerifier(
    issuer="https://your-idp.example.com",
    audience="https://your-mcp-server.example.com",
    jwks=HttpJwks("https://your-idp.example.com/.well-known/jwks.json"),
)
app = Hallpass(verifier=verifier, vault=CredentialVault(Fernet.generate_key()))
app.add_connector(YourConnector())            # a class with a `service` and a `tools()`
result = app.call_tool(bearer_token, "read_note", {})
```

A complete, self-contained, runnable example is [`examples/minimal.py`](../examples/minimal.py);
[`tests/test_end_to_end.py`](../tests/test_end_to_end.py) is the same idea as a test.
