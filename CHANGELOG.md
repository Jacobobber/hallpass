# Changelog

All notable changes to hallpass. This project follows [semantic versioning](https://semver.org): from 1.0.0 on, the public API (everything exported from the top-level `hallpass` package) is stable, and breaking changes bump the major version. Per-release detail is in the [GitHub releases](https://github.com/Jacobobber/hallpass/releases).

## [1.4.0]

- **Circuit breaker** (`CircuitBreakerHttpClient` / `BreakerPolicy`) — wrap any `HttpClient` (compose it around the retry client) and, after `failure_threshold` consecutive outages (5xx or connection-level errors) to a service, the breaker opens and calls fail fast with `CircuitOpen` for `reset_after` seconds; then one half-open probe closes it on success or re-opens it. Backpressure so a fleet of agents can't hammer a struggling downstream. Client errors (401/403/404) are real answers, not outages, and never trip it; a success resets the count. The clock is injected. Additive.

## [1.3.0]

Performance, informed by how the popular harnesses stay fast and measured with a new `evals/benchmark.py`:

- **Pooled HTTP client** — `HttpxClient` now holds one `httpx.Client` and reuses it across calls (keep-alive connection pooling), instead of a fresh connection per request. Repeated calls to a service skip the TCP + TLS handshake; ~5x on loopback, more over real TLS. Gains `close()` and an optional `http2=` flag.
- **JWT verify cache** — `TokenVerifier` caches a verified `Principal` keyed by the token until its `exp`, skipping the RSA signature check on a repeated bearer (~160x cheaper on the hot path). Safe (a token verifies identically until expiry; no revocation list is consulted). Bounded; `cache_size=0` disables it.
- **`evals/benchmark.py`** — throughput micro-benchmarks for verify (cached vs uncached), gating, the FLEX codec, and the vault, plus a loopback pooled-vs-unpooled HTTP comparison. Additive.

## [1.2.0]

- **Agent spawning** (`hallpass.agents`) — the orchestrator can now create scoped agents, not only dispatch to existing ones. `AgentSpec` describes an agent (name, harness scopes, task); `Team.spawn` mints it a token carrying only those scopes and launches it through a pluggable `Spawner` (default `SubprocessSpawner`), passing name/token/task/channel by environment; `AgentContext.from_env()` picks that up inside the spawned process. Each spawned agent is a scoped identity, not a trusted one, so its harness is the capability boundary (enforced at call time, audited). hallpass stays model-agnostic: it provisions the identity and harness and launches the process; the loop inside is yours. Demos: `examples/spawn_agents.py` and `examples/spawned_agent.py`. Additive.

## [1.1.0]

- **Orchestrator harness** (`hallpass.orchestrator`) — an agent that drives worker agents, composed from the existing primitives. `Orchestrator.dispatch(worker, do, args=...)` posts a FLEX `task` addressed to a worker and tagged with an id; `Worker` runs a registered handler for the task's operation and posts a `result` tagged with the same id; `Orchestrator.gather(ids)` matches results back. It rides `A2ABus`, so it is scope-gated (the harness does not bypass the auth core), durable (a worker can die mid-task and see it on reconnect), and audited. At-least-once delivery with `gather` de-duplicating by id; a failed handler reports only the exception type. Runnable demo: `examples/orchestrator.py`. Additive; no breaking changes.

## [1.0.0]

First stable release. The public API is now committed to under semver.

hallpass is the multi-user auth core that public MCP servers are missing: it answers who a request is from, where each user's downstream credentials live, and which tools *this* user gets — enforced at call time, not just in the catalog. What that means, as of 1.0:

### Identity, credentials, gating (the core)
- **`TokenVerifier`** — OAuth 2.1 resource-server verification: RS256 against a JWKS, exact issuer/audience, `alg=none` and symmetric algorithms refused, single JWKS refresh on unknown kid then fail closed. Injected JWKS source (HTTPS in production, static in tests).
- **User vs service principals** — `Principal.kind` / `is_service`, derived from a configurable token claim (Auth0 `gty`, Azure `idtyp`); descriptive, never a permission.
- **`CredentialVault`** — per-`(subject, service)` Fernet-encrypted storage; no secret in a repr, log, or error; operator-supplied key.
- **`ToolGate`** — scope-derived gating enforced at call time; an ungranted tool is indistinguishable from a nonexistent one.

### Connecting users (OAuth)
- **`OAuthConnect`** — the authorization-code connect flow (single-use state, PKCE), token exchange and refresh, tokens stored where the connector reads them; 22 catalog services carry prewired provider endpoints.
- **Self-healing** — `attach_refresh` renews a stale token and retries once on 401/403; `valid_token` refreshes proactively.
- **Consent & revoke** — a `ConsentLedger` records grants; `disconnect` clears the token, the refresh bundle, and the record.
- **`SqlitePendingStore`** — shared OAuth state for multi-instance deployments.

### Prewired connector catalog
- **47 services / 115 tools** as declarative `RestService`s (no per-vendor SDKs). Auth styles: bearer/token/bot/basic, header/query, templated, and multi-credential (JSON bundle → several headers). Form-encoded bodies (Stripe writes) and first-class GraphQL operations (Linear) are supported. Per-tenant services take a base URL.
- **Reliability** — transient-error retry with backoff honoring `Retry-After`, and an opt-in response-size guard.
- **MCP tool annotations** — read-only / destructive / idempotent hints, auto-derived from the HTTP verb.

### Agent-to-agent
- **`A2ABus`** — authenticated, authorized, durable channels (append-only log, forward-only ack, catch-up on reconnect).
- **Untrusted-message sanitization** — escape/control/bidi/zero-width stripping and an injection-resistant framing helper.
- **FLEX** — a token-efficient message language (~44% smaller than JSON for a representative message).

### Operations
- **Audit** — every allow and deny recorded (never a credential); `SqliteAuditLog` is durable and queryable, and `duration_ms` makes the trail a latency source (no separate observability dependency).
- **Rate limiting**, **connector availability**, **idempotency keys** (at-most-once retries), and **`doctor()`** (config self-check).

### Transports & tooling
- **MCP adapter** (thin, over the official SDK) and a **dependency-free HTTP reference server** (`hallpass serve`), plus a **`hallpass` CLI** (`serve` / `doctor` / `catalog`).
- **Tool search** — gate-enforced, benchmarked against a keyword baseline.

### Quality
- Property-based auth-isolation evals (Hypothesis), a tool-search benchmark, mypy `--strict`, ruff, and a Linux+Windows × Python 3.10–3.14 CI matrix. Every security property is enforced by the core and covered by a test that names the failure it prevents.
