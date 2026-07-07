# Ideas and roadmap

Candidates, not commitments. Where hallpass could go next, kept in the repo so
the direction is visible and ideas do not get lost. An item moves to "done"
only when it is built and tested.

## Done

- Identity (OAuth 2.1 resource-server verification), credential vault, scope gating, MCP adapter.
- Operational layer: audit trail (denials included), per-principal rate limiting, connector availability.
- Agent-to-agent channels: authenticated, authorized, durable.
- Tool search ("context to search"): gate-enforced, Unicode-aware, pluggable ranker.
- Batteries-included setup: `ToolKit` decorator connectors, `build()` / `dev_app()`.
- Prewired connector catalog: declarative REST framework + a growing catalog (see docs/CATALOG.md), with bearer / token / bot / basic / header / query auth and per-tenant base URLs.
- Per-provider OAuth connect flow (`OAuthConnect`): authorization-code with single-use state and PKCE, code exchange and refresh, tokens stored in the vault where the connector reads them; 20 catalog services carry prewired OAuth endpoints via `catalog.oauth_provider`.
- Self-healing token refresh: `OAuthConnect.attach_refresh(connector)` renews a stale token and retries once on a 401/403 (non-auth errors are not retried, and a no-op refresh does not loop); `OAuthConnect.valid_token()` refreshes proactively when the stored token is expiring. `ConnectorError` now carries the HTTP `status`.

## OAuth follow-ups (surfaced while building it)

- **Shared pending store**: `InMemoryPendingStore` is single-process; behind a load balancer start and finish can hit different instances, so ship a store the operator backs with Redis or a table (the protocol is already there).
- **Provider quirks**: some token endpoints nest or rename fields (Slack nests the token); a per-provider response adapter would absorb the odd ones. The registry covers URLs and scopes today.
- **State bound to the browser session**: binding state to the operator's session cookie would harden against cross-user state replay.

## Grow the catalog toward comprehensive

Adding a bearer/JSON service is a ~10-line declaration in `catalog.py`, so
breadth is mostly data entry. Known gaps the current framework does not yet
cover, each a small framework addition:

- **Form-encoded bodies** (Stripe, some legacy APIs): add a body-encoding option to `RestService`.
- **GraphQL** (Linear, monday, GitHub v4): supported today as a single POST endpoint with a `query` body; a small GraphQL helper (named operations, variables) would make it first-class.
- **Multi-credential services** (Twilio account SID + token, Datadog API + app key, Twitch token + client id): the vault stores one credential per (user, service); support a small credential bundle per service.
- ~~**Non-standard token placement** (PagerDuty `Authorization: Token token=...`): a templated auth style, e.g. `("template", "Token token={cred}")`.~~ **Done** — the `("template", "Token token={cred}")` auth style renders the credential into any Authorization scheme; PagerDuty, Square, Bitbucket, Stripe (read), and Freshdesk were added on the back of it (catalog now 46 services / 106 tools).

## Reliability and correctness

- ~~**Retry/backoff and rate-limit awareness** in the default HTTP client (honor `Retry-After`).~~ **Done** — `RetryingHttpClient` decorates any `HttpClient`: transient statuses (429, 5xx) retry with exponential backoff, obeying `Retry-After` when present; 401/403 are deliberately left to the connector auto-refresh. It is the default for the real network client. `ConnectorError` carries `status` and `retry_after`.
- **Response guard**: cap and paginate large tool/connector responses so a big downstream payload cannot blow the agent's context (learned the hard way: silent truncation loses data, so paginate rather than cut).
- **Idempotency on tool calls**: an optional idempotency key so an agent retrying a mutating call does not double-execute it.
- **`doctor()` self-check**: assert config invariants (JWKS reachable, vault key valid, no connector declaring impossible scopes, catalog doc fresh).

## Identity and access

- **Service (machine-to-machine) identities**: alongside user-delegated OAuth, let an agent authenticate as a service (client-credentials) with its own scopes.
- **Consent records + revoke**: explicit per-user, per-service consent that can be listed and revoked, beyond what the token scopes imply.
- **A2A depth**: seats and seat policy (durable per-(channel, role) membership with self-service rebind), presence/live-roster, server-side catch-up and orphan sweep.
- **A2A message sanitization**: render channel message bodies through a sanitizer (strip control characters, frame as untrusted) before they reach a model, since agents will read each other's messages.

## Evaluation (keep it honest)

- **Auth-isolation fuzzing**: adversarial suites that try to break cross-user and scope isolation under generated inputs.
- **Tool-search quality benchmark**: a controlled measure of whether a ranking change surfaces the right tool more often, against a naive baseline. Run the control that can overturn the result.

## Architecture: one package or an umbrella?

hallpass now spans agent-to-tools, agent-to-agent, tool search, and a connector
catalog, which is past what "auth core" describes. Two directions, to decide
deliberately rather than by accretion: keep growing hallpass as one harness, or
split it (hallpass = auth core; separate packages for delivery, connectors,
search) once the surface is large enough that the split pays for itself. Do any
rename as a single deliberate move, not incrementally.
