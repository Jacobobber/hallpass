# Platform: where this is heading

> **Status: vision + roadmap, not shipped.** This document describes what hallpass is a substrate *for*,
> and the phased path to get there. The library today is a single-node, in-process substrate; the
> "platform" is the deployable system you would build on it. Everything here is design and direction —
> when a section describes code that exists it links the module; otherwise it is a plan, not a promise.
> The honest one-line status of the library is in the [README](../README.md).

hallpass grew from an auth core into a substrate that already carries identity, credentials, gating,
authorized channels, orchestration, routing, a durable queue, and scoped spawning. A *platform* adds the
two things a fleet needs that a library deliberately leaves out: a governance boundary above the
per-user one, and shared-backend durability so it runs on more than one box. The design principle
throughout: **add the platform surface without touching the security spine** (`core.py`'s verify → gate),
so the tested auth invariants never drift.

## The layer stack

```
 ============================= PLATFORM (design) =============================
 (d) CONTROL PLANE      provisioning · registry · lifecycle · dashboard/admin API
 (c) ORG / GOVERNANCE   roles · delegation · seats · non-author approval · SoD · human gates
 (b) HARNESS SDK        per-agent-type runtimes · minter-as-service · self-registration
 (a) IDENTITY PLANE     per-org keys · agent key issuance / rotation / revocation
 ----------------------------------------------------------------------------
 ============================= SUBSTRATE (exists today) =====================
   core (verify→gate) · A2ABus · Orchestrator/Router · TaskQueue · agents/Spawner ·
   runner · audit · consent · oauth · catalog · vault · identity · gating
 (e) ENTERPRISE BACKENDS  SQLite → Postgres/Redis · a vault backend seam (the biggest lift)
```

Each platform layer is a thin addition over a seam that already exists — see
[agent-identity-and-organization.md](agent-identity-and-organization.md) for the identity/harness/org
design these layers implement.

## What exists vs. what's next, per layer

- **(a) Identity plane.** *Exists:* `TokenVerifier` (RS256/JWKS, service-kind), `CredentialVault`,
  `OAuthConnect` + `ConsentLedger`. *Add:* a per-org key domain (per-org Fernet key or KMS-per-org so
  cross-org reads are unrepresentable, the way cross-user already is); agent-identity rotate/revoke/reap;
  the provider's own token-revocation on disconnect.
- **(b) Harness SDK.** *Exists:* `AgentSpec`/`Team`/`Spawner`/`SubprocessSpawner`, the `HALLPASS_AGENT_*`
  env contract, `AgentContext.from_env()`, the `run_worker`/`serve_queue` loop shells. *Add:* promote
  the minter from a bare callable into an `AgentMinter` *service* backed by the IdP's client-credentials
  flow (this is where "own identity, own keys" becomes enforced rather than conventional); harness
  presets as first-class registry entries; boot-time self-registration to channel + router.
- **(c) Org / governance.** *Exists (to build on):* scope-as-capability everywhere, `ConsentLedger` as
  the durable-record pattern, presence/roster, the audit trail. *Add (net-new):* roles, delegation,
  seats, non-author approval, separation of duties, and human gates — each a durable record mirroring
  `SqliteConsentLedger`, each a scope/identity check at mint or call time, each audited.
- **(d) Control plane.** *Exists:* `Team.spawn`, roster + `Router` discovery, `TaskQueue` distribution,
  `SqliteAuditLog.query` observability, the HTTP/CLI surface. *Add:* a lifecycle supervisor (rotate,
  revoke, reap the fleet — behind a `ContainerSpawner`/`K8sSpawner` that implements the existing
  `Spawner` protocol), a shared cross-replica registry, and a gated admin + observability API/dashboard
  off the audit trail.
- **(e) Enterprise backends.** *Exists:* one uniform SQLite pattern (WAL + one lock + one connection +
  indexed hot queries) across six stores, several already pluggable Protocols. *Add:* in dependency
  order below.

## Scaling: measured, and the bounded path to multi-replica

The scalability of the substrate today, from a real audit (concurrency measured, not asserted):

- **Single user / single-node team — strong.** Correct under contention: 1,000 queue tasks across 8
  threads drained exactly-once at ~1,000 tasks/sec (zero double-claims); 1,600 A2A posts across 8
  threads produced a contiguous, gap-free sequence; a two-process test on one file held exactly-once.
  Durable across restart.
- **Enterprise multi-replica — bounded backend-swap work, not a rewrite.** The stateless auth path
  (verify → gate → vault-read → connector call) scales horizontally as-is. The SQLite coordination
  substrate is single-node and needs networked backends, in this order:
  1. **Shared `IdempotencyStore` + `RateLimiter` (Redis), before any load-balancer fan-out** — both
     defaults are per-process and fail *silently* on a second replica (at-most-once voids; the
     per-subject budget becomes N× the cap). This is the correctness gate for everything else.
  2. **A2A channel policies out of the per-process dict** into shared storage, or authz diverges across
     replicas.
  3. **A Postgres backend for `A2ABus` + `TaskQueue`** (`INSERT … RETURNING` for the monotonic sequence,
     `SELECT … FOR UPDATE SKIP LOCKED` for the claim) — removes the single-node coordination ceiling.
  4. **Re-back `CredentialVault` onto a shared DB / KMS-per-org** — the biggest single lift, because it
     is the one stateful store with no injectable-backend seam today (only its file path is
     configurable). Do it as its own deliberate change.

## Phased roadmap

**Phase 1 — Identity hardening + Harness SDK. ✅ Complete (v1.11.0–v1.15.0).**
*Landed:* the **`ProvisioningGuard`** (v1.11.0) — a `Team` given one verifies each minted token and
refuses to launch an agent that is not its own scoped **service** identity (subject == name, scopes ==
harness), closing the one misprovisioning path by which a spawned agent could act with a human's
identity; `dev_app` mints service tokens and exposes the app's verifier so it works out of the box.
**harness presets** (`Harness` / `HarnessRegistry`, v1.12.0) — `AgentSpec.harness` resolves to a
declared preset and an agent's scopes are bounded to it before minting, so an agent type's capability
ceiling is declared once. **minter-as-service** (`AgentMinter` / `ClientCredentialsMinter`,
v1.13.0) — each agent obtains its *own* service token from the IdP's client-credentials grant (one
OAuth client per agent), so "own identity, own keys" is a production code path, not a dev signer. And
**boot-time self-registration** (`join_channel`, `AgentContext.scopes`/`.principal()`, v1.14.0) — a
spawned agent carries its own scopes, reconstructs its principal, and announces itself onto its channel
(and, in-process, registers with a `Router`) so an orchestrator discovers it live without
pre-configuring it. And **agent lifecycle** (`Team.reap` / `terminate` / `rotate`, v1.15.0) — reap
exited agents, terminate one by name, and rotate an agent's identity (re-mint + re-launch under the
same spec, re-running the guard). Downstream *credential* revocation stays the operator's call (kill
the IdP client / `OAuthConnect.disconnect`). **Phase 1 is done — the next block is P2 governance.**
*Milestone:* an agent boots, obtains its *own* service credential from the IdP (no dev minter, no
operator token), self-registers, runs a task under its scoped harness, and can be rotated and revoked
out of band — all audited.

**Phase 2 — Org / governance.** Roles, delegation, seats, non-author approval, separation of duties,
human gates.
*Milestone:* a destructive task requires an approving human principal distinct from the requester, is
held pending until that human decides, and the chain is reconstructable from the audit trail — with a
named test asserting an author cannot self-approve.

**Phase 3 — Enterprise backends.** Redis cross-cuts + shared A2A policies first, then the Postgres
backend for the coordination stores, then the vault backend seam + shared/KMS-per-org vault.
*Milestone:* N replicas behind a load balancer with rate-limit, idempotency, A2A authz, coordination,
and per-org credentials all correct across replicas — verified by a named multi-replica isolation test.

**Phase 4 — Control plane / dashboard.** Lifecycle supervisor, shared registry, gated admin +
observability API/dashboard.
*Milestone:* an org admin provisions a harness, watches the live roster and queue depth, approves a
held human-gate, and revokes an agent — from the dashboard, every action gated and audited.

**The highest-leverage first move** is the minter-as-service in Phase 1: it is the physical seam where
*both* hard requirements — own identity, own keys — become enforced properties instead of conventions,
it is small, it unblocks the whole governance layer (roles and delegation are scope decisions made at
mint time), and it changes nothing in the security spine.

**Cross-cutting guardrail:** the test suite is the spec. Every new isolation property (the org
boundary, replica correctness, separation of duties) ships as a named test the way the current auth
invariants do — or the boundary quietly drifts.

## Non-goals (for now)

Async/uvloop — the core is synchronous and the per-call CPU cost is already small; connection reuse and
the verify cache are where the real time was. A bundled model runtime — hallpass provisions identity,
harness, and the loop shell; the thinking inside an agent stays the operator's. Being an identity
provider or an MCP framework — bring any OIDC issuer; the MCP adapter is a thin optional extra.
