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

## The biggest lever: per-provider OAuth connect flow

Today a connector needs the user's downstream token already in the vault; the
operator puts it there. The missing piece that makes the catalog usable end to
end is a per-provider OAuth connect flow: the user clicks "connect GitHub",
completes the provider's OAuth, and the access token lands in the vault under
that service, with refresh handled. This is what turns "N declared connectors"
into "a user connects and it works," and it is the single highest-value next
build. It pairs with the catalog: each service gains its authorize/token URLs
and scopes as more declarative data.

## Grow the catalog toward comprehensive

Adding a bearer/JSON service is a ~10-line declaration in `catalog.py`, so
breadth is mostly data entry. Known gaps the current framework does not yet
cover, each a small framework addition:

- **Form-encoded bodies** (Stripe, some legacy APIs): add a body-encoding option to `RestService`.
- **GraphQL** (Linear, monday, GitHub v4): supported today as a single POST endpoint with a `query` body; a small GraphQL helper (named operations, variables) would make it first-class.
- **Multi-credential services** (Twilio account SID + token, Datadog API + app key, Twitch token + client id): the vault stores one credential per (user, service); support a small credential bundle per service.
- **Non-standard token placement** (PagerDuty `Authorization: Token token=...`): a templated auth style, e.g. `("template", "Token token={cred}")`.

## Reliability and correctness

- **Response guard**: cap and paginate large tool/connector responses so a big downstream payload cannot blow the agent's context (learned the hard way: silent truncation loses data, so paginate rather than cut).
- **Idempotency on tool calls**: an optional idempotency key so an agent retrying a mutating call does not double-execute it.
- **Retry/backoff and rate-limit awareness** in the default HTTP client (honor `Retry-After`).
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
