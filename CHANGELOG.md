# Changelog

All notable changes to hallpass. This project follows [semantic versioning](https://semver.org): from 1.0.0 on, the public API (everything exported from the top-level `hallpass` package) is stable, and breaking changes bump the major version. Per-release detail is in the [GitHub releases](https://github.com/Jacobobber/hallpass/releases).

## [1.26.0]

- **A2A message-log backend seam** (`A2AStore` / `SqliteA2AStore` / `InMemoryA2AStore`) — Phase 3, and the last store in the bus without a swappable backend. `A2ABus` kept its message log, read cursors, and presence in a hardcoded SQLite connection; those now live behind an `A2AStore` protocol so a multi-replica fleet can point every bus at a shared database, while the bus keeps the authorization, auditing, and read-time sanitization above it (the store holds raw bytes and never inspects scopes). Each backend keeps the two guarantees its own way: `append` assigns a **monotonic, gap-free per-channel sequence** under the engine's serialization (SQLite: `BEGIN IMMEDIATE`), and `advance_cursor` is **forward-only** so a stale ack cannot regress a reader. `A2ABus(path=…)` defaults to `SqliteA2AStore` (public API and behavior unchanged); `A2ABus(store=…)` takes any backend. The seq-uniqueness guarantee is now asserted under real thread contention (200 posts, 8 threads, gap-free 1..n, none colliding) over both stock backends, and two buses on one shared store see each other's messages. Channel *policy* storage stayed its own `ChannelPolicyStore` (added in 1.22) so authorization is shareable even when the log is not. Additive; `A2ABus()` with no `store=` is byte-for-byte the old behavior.

## [1.25.0]

- **Postgres backends** (`PostgresTaskQueueBackend` / `PostgresChannelPolicyStore` / `PostgresVaultBackend`) — the concrete multi-replica backends the seams were built for. They implement the same protocols as the SQLite defaults, so a fleet points at Postgres by swapping the backend at construction, nothing else. The queue's `claim` is `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …` — the standard exactly-once work-queue pattern, so many workers on many replicas pull one backlog with each task going to exactly one worker and no global lock. Postgres is an optional dependency (the `postgres` extra, `psycopg`); the import is deferred, so a core install is unaffected. **Integration-tested against a real Postgres 16**, including the SKIP-LOCKED exactly-once claim under 8 concurrent connections (150 tasks, none double-claimed or lost); a new CI `postgres` job runs those tests against a Postgres service container on every push, while the default suite skips them (no `HALLPASS_TEST_DATABASE_URL`). Additive. (Testing against a real engine immediately caught a bug SQLite hides: `do` is a reserved word in Postgres and must be quoted.)

## [1.24.0]

- **Task-queue backend seam** (`TaskQueueBackend` / `SqliteTaskQueueBackend` / `InMemoryTaskQueueBackend`) — Phase 3, fourth increment. `TaskQueue` is now a thin facade over a `TaskQueueBackend`, so the durable work queue can be stored elsewhere — a shared database for a multi-replica fleet — without changing its semantics. The SQLite backend keeps the current exactly-once claim (write-locked `BEGIN IMMEDIATE`); the interface is documented so a Postgres backend implements `claim` with `SELECT … FOR UPDATE SKIP LOCKED`, each backend keeping the guarantee its own way. `TaskQueue(path=…)` defaults to `SqliteTaskQueueBackend` (public API and behavior unchanged); `TaskQueue(backend=…)` takes any backend. The exactly-once guarantee is now asserted under real thread contention (300 tasks, 8 workers, none claimed twice or lost) over *both* the SQLite and in-memory backends, so the abstraction demonstrably preserves it. Additive.

## [1.23.0]

- **Vault backend seam** (`VaultBackend` / `SqliteVaultBackend` / `InMemoryVaultBackend`) — Phase 3, third increment, and the single largest item the scalability audit named: `CredentialVault` was the one stateful store with no swappable backend (hardcoded `sqlite3`, only the file path configurable). Now `CredentialVault` owns the Fernet encryption and delegates *where the ciphertext lives* to a `VaultBackend`, so a multi-replica deployment can point every replica at a shared database (or a KMS-backed store) without moving the encryption boundary — the backend only ever sees opaque ciphertext, never a plaintext secret or the key. `CredentialVault(key, path=…)` defaults to `SqliteVaultBackend` (public API and behavior unchanged); `CredentialVault(key, backend=…)` takes any backend. Encryption, cross-subject isolation, no-secret-in-repr/log/error, and `durable` all stay exactly as before — the refactor is behind an unchanged surface, and every vault/OAuth/e2e test passes unmoved. Additive.

## [1.22.0]

- **Shared A2A channel-policy store** (`ChannelPolicyStore` / `InMemoryChannelPolicyStore` / `SqliteChannelPolicyStore`) — Phase 3, second increment. `A2ABus` kept its channel policies in a per-process dict, so behind a load balancer each replica had to re-declare every channel and would opaquely 404 on any it hadn't — channel *authorization* diverged across replicas, not just message data. The policies now live behind a `ChannelPolicyStore` protocol: `A2ABus(policies=…)` defaults to the in-memory store (unchanged behavior), or takes a shared `SqliteChannelPolicyStore` so a channel declared once is authorized identically on every bus pointed at it. Purely a seam extraction — the authorize/post/read/ack paths were already reading through `.get(channel)`, so only `declare_channel` and `channels` changed, and all existing A2A/presence/DM/orchestrator tests pass unmoved. Additive; `A2ABus()` with no `policies=` is byte-for-byte the old behavior.

## [1.21.0]

- **Shared cross-replica backends: Redis idempotency + rate limiting** (`hallpass.redis_backends` — `RedisIdempotencyStore` / `RedisRateLimiter`) — the first **Phase 3 (enterprise backends)** increment, and the correctness gate the scalability audit flagged for *any* load-balancer fan-out. The in-process `InMemoryIdempotencyStore` and `FixedWindowRateLimiter` fail silently on a second replica (each keeps its own cache and counters, so a retry that lands elsewhere re-runs the mutation and the per-subject budget becomes N× the cap). These put both in one shared Redis, satisfying the same `IdempotencyStore` / `RateLimiter` protocols, so swapping is a one-line change at `Hallpass(...)`. Redis is an optional dependency (the `redis` extra); the import is deferred, so a core install is unaffected, and each class takes an injected client (`from_url` builds a real one) — the whole suite tests them against a fake, no real Redis needed. Honest scope: idempotency is get-then-put (a *simultaneous* first-call race remains, as with in-memory; the common retry-after-record case is now strict cross-replica), and the rate limiter is a fixed window (the shared cap is the point). Additive.

## [1.20.0]

- **Human gates** (`hallpass.humangate` — `Gate` / `HumanGateLedger` / `InMemoryHumanGateLedger` / `SqliteHumanGateLedger`) — Phase 2, fifth increment; with it **Phase 2 (governance) is complete**. Some envelope changes must stay a human's call (granting/widening a capability, approving an irreversible or outward action, onboarding/offboarding an identity, key custody, a break-glass override). A human gate makes that structural: `require(gate_id, reason=…)` holds an action `pending`, and `decide(gate_id, principal, approved=…)` **raises `HumanGateError` if the principal is a service** (an agent can never clear a human gate) and records who decided, so the decision is attributable. A decision is final (re-deciding raises); `pending()` lists what awaits a human; `cleared()` is true only on approval. In-memory (thread-safe) and durable (`SqliteHumanGateLedger`, WAL, indexed by status) behind one protocol — a gate opened before a restart is still pending after it. Ties the governance layer to `Principal.is_service`: agents propose and execute within a granted envelope; only humans change the envelope. Additive.

## [1.19.0]

- **Separation of duties / non-author approval** (`hallpass.approvals` — `ApprovalLedger` + `separation_of_duties`) — Phase 2, fourth increment; the governance centerpiece. An author never approves its own work, enforced two ways in the same scope vocabulary. **At approval time**: `ApprovalLedger.record(artifact, approver, author=…)` raises `ApprovalError` if the approver is the author, and counts *distinct* approvers, so `approved(artifact, min_approvals=2)` means two different principals signed off. **At provisioning time**: `separation_of_duties(scopes)` is a pure check returning every artifact for which a scope set holds *both* `author:<X>` and `approve:<X>` — refuse a role, harness preset, or minted token whose scopes make that set non-empty and no principal can ever be positioned to approve its own work. In-memory (thread-safe) and durable (`SqliteApprovalLedger`, WAL) ledgers behind one protocol; the approval trail is queryable after the fact. Additive.

## [1.18.0]

- **Seats** (`hallpass.seats` — `Seat` / `SeatLedger` / `InMemorySeatLedger` / `SqliteSeatLedger`) — Phase 2, third increment; the durable counterpart to soft presence, and the "A2A depth: seats" roadmap item. Presence (`A2ABus.announce`/`roster`) is soft liveness that ages off; a **seat** is durable org structure — "who holds role R on channel C" — that survives a restart and changes only by an explicit `bind`/`unbind`/rebind, not a missed heartbeat. One holder per `(channel, role)`; `bind` is self-service rebind (a new subject replaces the previous holder); `holder`, `seats(channel)`, and `held_by(subject)` read it back. So a fleet has a *stable* org chart layered over the live view presence gives. In-memory (thread-safe) and durable (`SqliteSeatLedger`, WAL, indexed) behind one protocol; the clock is injected. Additive.

## [1.17.0]

- **Delegation** (`hallpass.delegation` — `Delegation` / `DelegationLedger` / `InMemoryDelegationLedger` / `SqliteDelegationLedger`) — Phase 2, second increment. Where roles say what a principal may do standing, delegation is the temporary, scoped hand-off: one principal lends *a subset of its own* scopes to another, for a while, for a job. Two invariants make it governance rather than a back door: **scope narrowing** — `delegate` is given the grantor's current scopes and raises `DelegationError` on any attempt to hand out more, so authority only ever shrinks down a delegation chain; and **expiry** — a delegation carries a TTL, and `active_scopes(grantee)` counts only unexpired grants (unioned across all grantors), so a temporary hand-off can't become standing by forgetting to revoke it. `revoke(grantor, grantee)` ends one early. Fold `active_scopes` into what you mint a subject's token with, alongside its roles. In-memory (thread-safe) and durable (`SqliteDelegationLedger`, WAL, indexed) behind one protocol; the clock is injected. Additive.

## [1.16.0]

- **Roles** (`hallpass.roles` — `Role` / `RoleStore` / `InMemoryRoleStore` / `SqliteRoleStore`) — the first **Phase 2 (governance)** increment. A harness is an agent *type's* scope ceiling; a role is a named scope set assigned to a *principal* (agent or human), and a subject's effective scopes are the union of the roles it holds. `define(Role)` sets a role's scopes, `assign(subject, role)` grants it (raising `RoleError` — a `KeyError` subclass — if the role was never defined, rather than a silent empty grant), and `scopes_for(subject)` resolves the effective set to mint the subject's token with. This makes "membership in a team = holding a role" the governance substrate, and an org change a role change rather than a per-agent scope edit — redefining a role re-derives every holder's effective scopes without touching assignments. `InMemoryRoleStore` is the thread-safe single-process default; `SqliteRoleStore` persists roles and assignments (WAL, indexed, mirrors the consent/vault storage pattern). Additive.

## [1.15.0]

- **Agent lifecycle** (`Team.reap` / `Team.terminate` / `Team.rotate`) — Phase 1, fifth increment; with it **Phase 1 (identity hardening + harness SDK) is complete**. `terminate(name)` stops a specific agent and drops it from tracking; `reap()` drops agents that already exited and returns their names (so the tracked set doesn't grow without bound); `rotate(name)` terminates the running instance and spawns a fresh one under the same spec, which re-mints its token (a new credential) and re-runs the harness bound and provisioning guard — identity rotation without re-supplying the spec. The `Team` now remembers each agent's latest spec to make rotation possible. Process lifecycle only; revoking an agent's downstream *credentials* stays the operator's call (kill its IdP client / `OAuthConnect.disconnect`). Additive — existing `agents` / `alive` / `shutdown` are unchanged.

## [1.14.0]

- **Boot-time self-registration** (`join_channel`; `AgentContext.scopes` / `AgentContext.principal()`) — Phase 1, fourth increment. A spawned agent now knows its own granted scopes: `Team.spawn` passes them in the environment (`HALLPASS_AGENT_SCOPES`) and `AgentContext` carries them, with `AgentContext.principal()` reconstructing the agent's `Principal` (name + scopes) without decoding the token. `join_channel(bus, ctx, router=...)` is the boot-time self-registration: the agent announces presence on its channel (so an orchestrator's roster sees it live) and, given an in-process `Router`, registers its capability so it can be routed work — without the orchestrator pre-configuring it. Previously a spawned agent had to rebuild an *empty*-scoped principal by hand (see the old `examples/spawned_agent.py`); it now self-registers with its real identity. Additive: `AgentContext.scopes` defaults empty when the env var is absent, so an agent launched by a pre-1.14 spawner still loads. `examples/spawned_agent.py` updated to use it.

## [1.13.0]

- **Minter-as-service** (`hallpass.minter` — `AgentMinter` / `ClientCredentialsMinter` / `AgentClient`) — Phase 1, third increment. A `Team`'s `mint` was a bare callable (a dev signer in practice); it now has a named `AgentMinter` protocol and a production implementation. `ClientCredentialsMinter` exchanges *each agent's own* OAuth 2.0 client credentials for a scoped service token at the IdP's token endpoint — one OAuth client per agent — so the token an agent carries is issued **to the agent**, never borrowed from a human's session and never a shared secret. An agent with no registered client-credentials identity is refused (no silent fallback to a shared identity). It's a callable `AgentMinter`, so it drops straight into `Team(mint=...)`; pair it with the `ProvisioningGuard` and the verifier still enforces that the minted token is the agent's own service identity. Reuses the OAuth `TokenHttp` seam, so the core install has no new dependency (the httpx client is the same optional extra) and it tests against a fake token client — no network, no real IdP.

## [1.12.0]

- **Harness presets** (`Harness` / `HarnessRegistry`) — Phase 1, second increment. `AgentSpec.harness` was a free-text label; it is now a lookup key into a `HarnessRegistry` of declared harness types, each a named maximum scope set for a *kind* of agent (reviewer, messenger, …). Give a `Team` a registry and a spawned agent's requested scopes must stay within its harness's preset — an agent asking for scope beyond its type raises `ProvisioningError` (naming the excess) and nothing launches; an unknown harness name is a misconfiguration, not a silent empty grant. So a harness means one thing, declared once, everywhere it is spawned. Composes with the `ProvisioningGuard`: the harness bound (scopes ⊆ preset, checked before minting) and the guard (minted token is the agent's own service identity with exactly those scopes, checked before launch) are independent. Additive and opt-in — a spec with no harness name, or a `Team` with no registry, behaves exactly as before.

## [1.11.0]

- **Provisioning guard** (`ProvisioningGuard` / `ProvisioningError`) — the first build increment toward the agent-org platform (Phase 1 in [docs/PLATFORM.md](docs/PLATFORM.md)). hallpass already makes a spawned agent unable to read another subject's credential (the vault is keyed by subject); the remaining gap was provisioning — nothing stopped an operator's `mint` callable from signing a *human's* subject, a user-kind token, or a widened scope set, any of which would let a spawned agent act with an identity that isn't its own. Give a `Team` a `ProvisioningGuard(verifier)` and each minted token is now verified *before* launch against the same verifier the server uses; unless it is a **service** principal whose **subject equals the agent's name** with **exactly the declared harness scopes**, the spawn raises `ProvisioningError` and nothing starts. A `require_service=False` opt-out covers deliberate user-kind cases. To make it usable out of the box, `build()`/`dev_app()` now accept `service_claim`/`service_values`, the dev minter takes `service=True`, and `Hallpass.verifier` is exposed read-only so a guard can be built from the running app's verifier. Additive and opt-in — a `Team` without a guard behaves exactly as before; the security spine (`core.py` verify → gate) is unchanged.

## [1.10.1]

- **Documentation re-architecture** (docs only; no API change). The project had grown from a "multi-user auth core for MCP servers" into a multi-agent auth *and* coordination substrate, but the docs still opened on the old thesis. Re-framed around what it is: an auth-native substrate for organizing fleets of independently-credentialed agents. The README is rewritten (~430 → ~150 lines) to lead on that thesis and index depth into new docs instead of inlining every feature; `docs/ARCHITECTURE.md` (the layer map), `docs/agent-identity-and-organization.md` (the org-scale design: API-key-per-agent identity, custom harnesses, the capability-graph org method, and the actions that must stay human), and `docs/PLATFORM.md` (the vision + phased roadmap, clearly labeled not-shipped) are new. The package docstring and the `pyproject` description/keywords are reframed to match; `SECURITY.md` now lists the coordination surface in scope; stale catalog counts in `docs/IDEAS.md` were corrected. No source behavior changed.

## [1.10.0]

- **Durable, thread-safe consent** (`SqliteConsentLedger`; `InMemoryConsentLedger` now locked). The scalability audit found the only `ConsentLedger` implementation was in-memory *and* had no lock — a durability gap (grants lost on restart) and a thread-safety bug under the threaded reference HTTP server, despite the docstrings promising a durable table. Fixed both: `InMemoryConsentLedger` now guards its map with a lock, and `SqliteConsentLedger(path=...)` persists grants behind the same `ConsentLedger` protocol (one connection, WAL, one lock — the vault/A2A/queue pattern), so a grant survives a restart and a revoke is durable. Wire it into `OAuthConnect(consent=...)` for a deployment that must remember consent across restarts or instances. Additive — `InMemoryConsentLedger` stays the default.

## [1.9.1]

- **SQLite persistence hardening** (performance; no public API change). WAL is now enabled uniformly across every SQLite-backed store (previously only `A2ABus` and `TaskQueue` set it; `CredentialVault`, `SqliteAuditLog`, and `SqlitePendingStore` used the default rollback journal), so concurrent readers no longer block the writer. Added indexes on the hot queries that were full table scans: `tasks(status, created_at)` for `TaskQueue.claim`/`outstanding` (the scan grew with accumulated done rows), `audit(subject)` and `audit(tool)` for `SqliteAuditLog.query`, and `a2a_presence(channel, last_seen)` for `A2ABus.roster`. Index usage is asserted with `EXPLAIN QUERY PLAN` in the suite, and no index was added where the planner wouldn't use it — a time-bounded audit query already terminates early on a reverse-`id` walk, so an `at` index would only cost writes on the highest-volume table. Migration-safe (`CREATE INDEX IF NOT EXISTS`); builds on existing data.

## [1.9.0]

- **Reference agent runners** (`hallpass.runner` — `run_worker` / `serve_queue`) — the reusable worker loop a served agent runs, shipped once instead of rewritten per agent. hallpass runs no model loop of its own (deliberate); what a spawned agent still needs is the loop *around* its work, and that loop is model-agnostic. `serve_queue(queue, worker, handlers, …)` claims from a `TaskQueue`, dispatches by operation to a handler, and completes each task — idempotent and lease-safe, with a raising handler reported as `ok=False` carrying only the exception *type* (never its message), mirroring the orchestrator `Worker`; an unknown operation completes `ok=False` note `"no handler"`. `run_worker(worker, …)` is the same loop over an A2A `Worker`/channel. Both take an optional `stop` predicate and `max_idle_rounds` (serve-forever or drain-and-return), an injectable `sleep` (so the loops test without wall-clock waits), and a `heartbeat` hook — wire `bus.announce` into it to hold a live-roster seat, tying serving and presence together. The auth boundary is unchanged: a handler acts with the agent's scoped token. Demo: `examples/reference_agent.py`. Additive.

## [1.8.0]

- **Direct messages** (`hallpass.dm` — `open_dm` / `direct_channel` / `DirectChannel`) — a private 1:1 channel between two agents, as an auth-native construct rather than a new transport. `direct_channel(a, b)` purely and deterministically derives an order-independent channel name and a single pair scope (a truncated SHA-256 of the NUL-separated sorted subjects, so `(a,b) == (b,a)` and distinct pairs don't collide); `open_dm(bus, a, b)` declares that channel on the bus with a policy requiring the pair scope to both post and read, and returns the descriptor. The caller mints each party a token carrying `descriptor.scope` — and that scope is the entire access control: a third party who learns the channel name still can't post or read (it lacks the scope, and the bus denies it opaquely, exactly like any other channel). Once opened it is an ordinary channel, so `post`/`catch_up`/`ack` and the live roster all work and stay gated to the pair. Idempotent to re-open; self-DMs are rejected. Demo: `examples/direct_messages.py`. Additive.

## [1.7.0]

- **Live roster / presence** (`A2ABus.announce` / `A2ABus.roster`) — who is on a channel right now. An agent heartbeats with `announce`; `roster(channel, within=…)` returns the subjects seen within the window, sorted. Gated by the same scopes as the messages: `announce` needs the channel's post scope (asserting presence is a write, so a read-only principal cannot claim a seat), `roster` needs the read scope, and denial stays opaque (who-is-here does not leak channel existence). Presence is soft state — a subject that stops heartbeating ages off, and being on the roster is never a grant — so an orchestrator can dispatch only to workers that are actually up and notice when one goes dark. Backed by a per-channel presence table on the existing bus connection; additive.

## [1.6.0]

- **Auth-native routing** (`Router`) — route a task to a worker by capability, where capability is the auth scope set. A worker registers its harness (granted scopes); a task declares the scopes it needs; `route` returns a worker whose harness covers them, round-robin across the eligible ones, or `None` if none is capable (visible, not a silent misroute). The differentiator the harness research called out: the same scopes that gate tool calls decide who is capable of a task, so work can't be routed to an agent that isn't authorized for it. Pair with `dispatch` or the task queue. Additive.

## [1.5.0]

- **Durable task queue** (`hallpass.taskqueue`) — `TaskQueue` on SQLite, closing the two gaps the harness research flagged (event-log resume, dedup + lease). `enqueue` writes a pending task; `claim` hands one worker exactly one task under a write-locked transaction (concurrent workers never claim the same one) and leases it; an expired lease makes an abandoned task claimable again (a dead worker doesn't strand it); `complete` is idempotent by id, so a re-run cannot overwrite a recorded result; `result` / `outstanding` survive a restart, so a resuming orchestrator sees what is still in flight. A coordination/durability primitive; the auth boundary stays on the tools a worker calls. Additive.

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
