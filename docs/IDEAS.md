# Ideas and roadmap

Candidates, not commitments. Where hallpass could go next, kept in the repo so
the direction is visible and ideas do not get lost. An item moves to "done"
only when it is built and tested.

## Done

- Identity (OAuth 2.1 resource-server verification), credential vault, scope gating, MCP adapter.
- Operational layer: audit trail (denials included), per-principal rate limiting, connector availability. `SqliteAuditLog` is a durable, queryable sink (`query` by subject/tool/decision/time); `AuditEvent.duration_ms` records call latency, so the audit trail doubles as an observability source without an OpenTelemetry dependency.
- Agent-to-agent channels: authenticated, authorized, durable.
- Agent spawning (`hallpass.agents`): the orchestrator creates scoped agents, not just dispatches to existing ones. `Team.spawn(AgentSpec)` mints each agent a token carrying only its harness scopes and launches it through a pluggable `Spawner` (default `SubprocessSpawner`), passing name/token/task/channel by environment; `AgentContext.from_env()` picks it up inside the process. Each agent is a scoped identity, not a trusted one; hallpass stays model-agnostic (the process's loop is yours). Demo: `examples/spawn_agents.py`.
- Orchestrator (`hallpass.orchestrator`): an agent that drives worker agents over an A2A channel. `Orchestrator.dispatch`/`gather` + `Worker` with registered handlers; tasks/results are FLEX messages tagged by id. Rides `A2ABus` so it is scope-gated, durable, and audited; at-least-once with gather de-dup; failed handlers report only the exception type. The coordination layer the auth core composes into.
- FLEX (`hallpass.flex`): a token-efficient A2A message language -- `<kind> [@recipient]* [#ref]* [key=value]* [ | note]`, ~44% smaller than compact JSON for a representative message, round-trips, tolerant parse, sanitized on the way in. Rides `A2ABus` unchanged (`encode`/`parse` around the string body).
- Tool search ("context to search"): gate-enforced, Unicode-aware, pluggable ranker.
- MCP tool annotations (`ToolAnnotations`): read-only / destructive / idempotent hints on every `ToolSpec`, auto-derived from the HTTP verb for catalog connectors, advertised through the MCP adapter and the HTTP server's `/tools`, so a client can warn before a destructive call. Advisory only; access stays scope-decided.
- Batteries-included setup: `ToolKit` decorator connectors, `build()` / `dev_app()`.
- Runnable server + CLI: `hallpass.http_server` is a dependency-free HTTP reference server (pure `handle_request` + stdlib `http.server`) that gates exactly like the core (per-bearer `/tools`, verified+gated `/call`, opaque 404, capped body, no credential/traceback leakage — security-reviewed); the `hallpass` CLI (`serve` / `doctor` / `catalog`) makes "clone and run a real multi-user server" one command.
- Prewired connector catalog: declarative REST framework + a growing catalog (see docs/CATALOG.md), with bearer / token / bot / basic / header / query auth and per-tenant base URLs.
- Per-provider OAuth connect flow (`OAuthConnect`): authorization-code with single-use state and PKCE, code exchange and refresh, tokens stored in the vault where the connector reads them; 20 catalog services carry prewired OAuth endpoints via `catalog.oauth_provider`.
- Self-healing token refresh: `OAuthConnect.attach_refresh(connector)` renews a stale token and retries once on a 401/403 (non-auth errors are not retried, and a no-op refresh does not loop); `OAuthConnect.valid_token()` refreshes proactively when the stored token is expiring. `ConnectorError` now carries the HTTP `status`.

## OAuth follow-ups (surfaced while building it)

- ~~**Shared pending store**: `InMemoryPendingStore` is single-process; behind a load balancer start and finish can hit different instances.~~ **Done** — `SqlitePendingStore` (file-backed, single-use with atomic pop, TTL) lets `start` and `finish` land on different instances. Mirrors the vault/A2A SQLite patterns. A Redis-backed store is still a valid drop-in via the same `PendingStore` protocol.
- **Provider quirks**: some token endpoints nest or rename fields (Slack nests the token); a per-provider response adapter would absorb the odd ones. The registry covers URLs and scopes today.
- **State bound to the browser session**: binding state to the operator's session cookie would harden against cross-user state replay.

## Grow the catalog toward comprehensive

Adding a bearer/JSON service is a ~10-line declaration in `catalog.py`, so
breadth is mostly data entry. Known gaps the current framework does not yet
cover, each a small framework addition:

- ~~**Form-encoded bodies** (Stripe, some legacy APIs): add a body-encoding option.~~ **Done** — an endpoint sets `form=True` to send its body as `application/x-www-form-urlencoded` (via a new `data=` on the HTTP client) instead of JSON; `HttpClient`/`HttpxClient`/`RetryingHttpClient` carry it, and `data=` is passed only for form endpoints so JSON-only clients never see it. Stripe's write endpoints (`stripe_create_customer`, `stripe_update_customer`) ship on the back of it.
- ~~**GraphQL** (Linear, monday, GitHub v4): a small GraphQL helper (named operations, variables) would make it first-class.~~ **Done** — `Endpoint.graphql` holds a fixed query document; the tool POSTs `{"query": <doc>, "variables": {<args>}}`, so a caller invokes a named operation (Linear now ships `linear_viewer` / `linear_my_issues` / `linear_teams` / `linear_issue`) instead of hand-writing GraphQL. Tool args become the variables and appear in the argument schema.
- ~~**Multi-credential services** (Twilio SID + token, Datadog API + app key): the vault stores one credential per (user, service); support a small credential bundle per service.~~ **Done** — the `("multi", ((placement, name, field), ...))` auth style: the user stores a JSON bundle in the single slot, and each field is placed in its own header/query parameter. Datadog (DD-API-KEY + DD-APPLICATION-KEY) ships on it; a malformed bundle is a clean `ConnectorError` and is never echoed.
- ~~**Non-standard token placement** (PagerDuty `Authorization: Token token=...`): a templated auth style, e.g. `("template", "Token token={cred}")`.~~ **Done** — the `("template", "Token token={cred}")` auth style renders the credential into any Authorization scheme; PagerDuty, Square, Bitbucket, Stripe (read), and Freshdesk were added on the back of it (catalog now 46 services / 106 tools).

## Reliability and correctness

- ~~**Retry/backoff and rate-limit awareness** in the default HTTP client (honor `Retry-After`).~~ **Done** — `RetryingHttpClient` decorates any `HttpClient`: transient statuses (429, 5xx) retry with exponential backoff, obeying `Retry-After` when present; 401/403 are deliberately left to the connector auto-refresh. It is the default for the real network client. `ConnectorError` carries `status` and `retry_after`.
- ~~**Response guard**: cap large tool/connector responses so a big downstream payload cannot blow the agent's context.~~ **Done** — `guard_response(value, max_bytes=...)` returns the value unchanged when it fits, else an explicit envelope (`hallpass:truncated`, byte counts, a UTF-8-safe preview, and guidance to re-query narrower) so overflow is never silent. Opt-in on `RestConnector`/`catalog.load(max_response_bytes=...)`. Deliberately does not auto-paginate: paging is the underlying tool's own limit/cursor params, which the envelope tells the model to use.
- ~~**Idempotency on tool calls**: an optional idempotency key so an agent retrying a mutating call does not double-execute it.~~ **Done** — `call_tool(..., idempotency_key=...)` with an injected `IdempotencyStore` (in-memory default, TTL) returns the first call's result on a repeat of the same `(subject, tool, key)`, so a retried mutation runs at most once. Only successful results are remembered; keys are scoped per subject and tool (no cross-user/cross-tool leakage); the check runs after authorization. In-memory store is best-effort under concurrency — a production store with atomic put-if-absent (Redis SETNX) makes it strict via the same protocol.
- ~~**`doctor()` self-check**: assert config invariants.~~ **Done** — `doctor(app)` returns findings (no tools = error; ephemeral vault, no audit, no rate limit, unavailable connectors = warnings), `format_report` renders them. Pure introspection, no network. Follow-up: an opt-in mode that probes the JWKS endpoint over the network.

## Identity and access

- ~~**Service (machine-to-machine) identities**: alongside user-delegated OAuth, let an agent authenticate as a service (client-credentials) with its own scopes.~~ **Done** — `Principal.kind` / `is_service`, derived by `TokenVerifier(service_claim=..., service_values=...)` from a configurable claim (Auth0 `gty`, Azure `idtyp`, ...). Descriptive, not a permission — access stays scope-decided, and marking a token a service does not relax any verification check. Enforcement (branching a tool on `is_service`) is the operator's.
- ~~**Consent records + revoke**: explicit per-user, per-service consent that can be listed and revoked.~~ **Done** — a `ConsentLedger` (in-memory default, injectable) records the granted scopes and time on `OAuthConnect.finish`; `connect.consents(subject)` lists them and `connect.disconnect(subject, service)` revokes, clearing BOTH the access token and the refresh bundle from the vault. Follow-up: call the provider's own token-revocation endpoint on disconnect where one exists.
- **A2A depth**: seats and seat policy (durable per-(channel, role) membership with self-service rebind), presence/live-roster, server-side catch-up and orphan sweep.
- ~~**A2A message sanitization**: render channel message bodies through a sanitizer before they reach a model.~~ **Done** — `sanitize()` strips terminal escape sequences (CSI/OSC/DCS/SOS/PM/APC), control chars, Unicode bidi overrides (Trojan-Source), and zero-width/invisible chars incl. the tag block (ZWJ/ZWNJ and emoji variation selectors preserved), and bounds length; `frame_untrusted()` wraps text in an injection-resistant data boundary (case/whitespace-tolerant defang, validated label). `A2ABus` sanitizes on read by default (storage stays raw). Hardened against an adversarial review (DCS passthrough, bidi, defang bypass, label injection all fixed). Honest scope: neutralizes spoofing/hiding, does not claim semantic injection detection.

## Evaluation (keep it honest)

- ~~**Auth-isolation fuzzing**: adversarial suites that try to break cross-user and scope isolation under generated inputs.~~ **Done** — `tests/test_properties.py` uses Hypothesis to assert the four core invariants across generated scope sets / subjects / queries: call-time gating (a tool runs iff the caller holds its scopes), vault isolation (no subject reads another's credential), search ⊆ authorized, and A2A read gating. A failing example prints the exact input that broke it.
- ~~**Tool-search quality benchmark**: a controlled measure of whether a ranking change surfaces the right tool more often, against a naive baseline.~~ **Done** — `evals/tool_search_benchmark.py` scores the shipped `LexicalRanker` against a naive keyword-overlap baseline on 18 labelled queries over the full catalog. Result: ranker MRR 0.944 / P@3 1.00 vs baseline MRR 0.814 / P@3 0.89 — the ranker genuinely beats keyword matching (and the harness prints an honest negative verdict if a future change makes it stop). `tests/test_search_benchmark.py` pins it as a regression guard.

## Architecture: one package or an umbrella?

hallpass now spans agent-to-tools, agent-to-agent, tool search, and a connector
catalog, which is past what "auth core" describes. Two directions, to decide
deliberately rather than by accretion: keep growing hallpass as one harness, or
split it (hallpass = auth core; separate packages for delivery, connectors,
search) once the surface is large enough that the split pays for itself. Do any
rename as a single deliberate move, not incrementally.
