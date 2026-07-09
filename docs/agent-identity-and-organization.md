# Agent identity and organization

The origin essay, [multi-user is the hard part](multi-user-is-the-hard-part.md), is about one server
serving many *users*. This is its sequel at the next scale: many *agents*, each its own identity,
organized into something that can be reasoned about and governed. It is a design argument — some of it
is built today, some of it is the roadmap in [PLATFORM.md](PLATFORM.md). Where a claim is about code
that exists, it cites the module; where it is about code that does not exist yet, it says so.

The thesis in one line: **an organization of agents is not a new system bolted onto an auth layer — it
is the auth layer, expressed at fleet scale.** Membership is holding a capability; a role is a scope
set; who may approve whom is a scope invariant; and the whole thing is enforced at call time and
recorded in one audit trail. If that is true, the hard question is not "how do agents coordinate" (that
is messaging and queues) but "what is an agent's *identity*, and how does an org of them stay
least-privilege and governable as it grows."

## Can a spawned agent act with the human's identity?

No — by construction, and this is the load-bearing property everything else rests on.

When `Team.spawn` creates an agent it mints a token whose **subject is the agent's own name**
(`agents.py`), and a tool call resolves its credential as `vault.fetch(principal.subject, service)`
(`connectors.py`) — keyed to that agent's subject, with **no cross-subject accessor** anywhere in the
vault. So when an agent calls, say, a "approve pull request" tool, the credential it presents to the
forge is whatever is vaulted under *the agent's* subject — its own bot token — and the approval is
attributed to the agent, not to the human who launched it. The human's credential lives under the
human's subject, in a different row the agent has no way to name. This is the same isolation that keeps
one user from reading another's credential; agents are just more subjects.

There is exactly **one** way to break it, and it is a provisioning mistake, not a design gap: if the
operator's minter signs a token with the *human's* subject, or vaults the *human's* API key under the
agent's subject, then the agent really would act as the human. hallpass cannot detect a minter that
lies about the subject — today it trusts the operator to provision honestly. Closing that is the first
thing on the roadmap (the **provisioning guard**, below): a check that runs before launch and asserts
the minted token is service-kind, its subject equals the agent's name, and its scopes equal the
declared harness. That converts "the operator must not misprovision" from a caveat into an enforced
invariant.

The deeper point is the one the guard makes structural: **an agent must carry its own API keys, never
ride a human's harness.** A "harness" that reuses a human's authenticated session (the human's `gh`
login, the human's model key) makes every action the agent takes land under the human — the audit trail
lies, the blast radius is the human's entire grant, and you cannot revoke the agent without cutting the
human off. Per-agent identity is the opposite on every axis: own model quota, own tool credentials,
blast radius bounded by the harness, revocation that touches nobody else, and an audit trail that names
the actor truthfully.

## The identity model: API-key-per-agent

Every agent is its own **service principal** (machine-to-machine, `is_service` true), minted via a
client-credentials flow with least privilege — the scopes it carries *are* its entire grant, there is
no ambient authority. An agent has three credential planes, and all three are the agent's own:

| Plane | What | Where it lives | Who reads it |
|---|---|---|---|
| **Token** (identity) | the scoped JWT that gates its tools and channels | minted at spawn, passed in the environment | the core, on every call |
| **Model-provider key** | the key its runtime uses to talk to its LLM *as the agent* | a reserved vault slot (e.g. `model:provider`) under the agent's subject | the harness, at startup |
| **Downstream-service keys** | bot/repo/API tokens for the tools it calls | vault slots under the agent's subject | tool handlers, via `UserContext.credential()` |

The model-provider key is the plane that lives *nowhere* in hallpass today, because hallpass runs no
model loop — and that is correct: the core never names a model vendor. It rides the same per-subject
vault as any other downstream secret; it is special only in that the *harness*, not a tool handler,
reads it. Two agents of the same type carry *different* model keys because they are different subjects,
so spend, rate limits, and revocation are per-agent.

**Lifecycle** rides primitives that already exist. *Issuance:* the provisioner creates the agent's IdP
client, seeds its vault slots, and records consent (`ConsentLedger`) so "what this agent may act on" is
listable and revocable — the sibling of the user-delegated `OAuthConnect.finish` path. *Rotation:*
token rotation is just re-mint with a short TTL (the verify cache expires at the token's own `exp`);
key rotation is a vault upsert; OAuth-backed keys self-heal. *Revocation:* kill the IdP client (short
TTLs mean the token stops verifying without a revocation list), `disconnect` the downstream tokens, burn
the model slot, terminate the process. Every step is audited through the same sink.

**Enforcement to add (roadmap):** a `ProvisioningGuard` on the spawn path (service-kind, subject ==
name, scopes == harness preset, preset ⊆ its parent's authority); an optional `require_service` flag so
agent-only tools refuse a human token even if one leaks; and a clean human-vs-service separation where
the *only* structural link between a human and an agent is that the human provisions it. Effort:
small-to-medium, and it changes nothing in the security spine — the core still just verifies whatever
token arrives.

## Custom harnesses

A **harness** is the per-agent-type runtime that runs *inside* a spawned agent's process — the thing
hallpass deliberately does not provide, because the model loop is yours. It is not a human's client
reused; it is a small program that:

1. loads `AgentContext.from_env()` — its own scoped token, task, channel, name;
2. loads its **own** model key from its vault slot and authenticates to its LLM as the agent;
3. calls tools through hallpass with its own token, so the core resolves its own vaulted downstream
   credentials;
4. runs the loop by extending `run_worker` / `serve_queue`, wiring `heartbeat` to `announce` for a live
   roster seat and a `stop` predicate for clean shutdown;
5. stops cleanly and lets the `Team` reap it.

The contract is narrow and its one invariant is: **a harness never reaches outside its own subject.**
It only ever uses its own token and its own vault slots. A `Harness` type is a *scope preset + a runtime
+ a model tier*, so the taxonomy doubles as the routing table (`Router` routes by the same scopes):

| Harness | Scope preset (illustrative) | Runtime | Notes |
|---|---|---|---|
| **reviewer** | `{code:read}` (+ an `approve:<artifact>` scope only when separation of duties allows) | `run_worker` on a review channel | reads and judges; never approves what it authored |
| **builder** | `{code:read, code:write, ci:run}` | `serve_queue` on a durable `TaskQueue` | long tasks, crash-recovery via the lease |
| **researcher** | `{search:read, web:fetch}` | `run_worker` | read-only; no outward-effecting scopes |
| **messenger** | `{chat:write}` | `run_worker` | narrow outward effect; often no model at all |

Spawning selects the harness: `AgentSpec.harness` becomes a lookup key into a registry that resolves the
preset (and the guard asserts the requested scopes are a subset of it), picks the runtime, and passes
the harness type to the launched process. hallpass stays model-provider-agnostic — a thin `ModelClient`
seam lives outside the core, behind an optional extra, the way the httpx and JWKS clients already defer
their imports.

## The organization: capability graph, run as a platform

The strongest way to organize agents is not an org-chart drawn on top of the auth layer; it is to
recognize that the auth layer *already is* the org, and add the two things missing above the per-user
boundary: an org/governance vocabulary, and shared-backend durability.

**Capability is the substrate.** Membership in a team = holding the capability = your token carries the
scope. Routing = capability match (`Router`). Channel access = channel scopes. There is no separate
membership store; the scope set is the fact. **Separation of duties** falls out as a scope invariant:
the `approve:<artifact>` capability is never co-held by the same principal that holds `author:<artifact>`
for that artifact — enforced at provisioning (the guard refuses a preset that unions both) and verifiable
forever after in the audit trail (query for any subject that both authored and approved the same ref).

**Delegation is scope narrowing.** A human root-of-trust delegates to orchestrators, which delegate to
workers and *independent* reviewers. Least privilege at each hop is a subset check: a parent can only
mint a child whose scopes are a subset of its own authority. Escalation runs the other way — actions
that are irreversible, outward-facing, or high-blast-radius carry a scope no agent preset includes (or
are flagged destructive), so they route *up* to a human-held capability rather than being taken by an
agent.

**Coordination maps onto the substrate that exists:** channels + policy are org boundaries (hard
isolation = separate channels with distinct read scopes); *seats* (roadmap) are the durable
per-`(channel, role)` membership that presence is the soft, live view of; DMs are private escalation
paths; and the audit trail is the flight recorder that makes delegation and separation of duties
*verifiable after the fact*, not just asserted.

**It runs as a platform:** provisioning (mint identity + keys + scopes from a spec), discovery (the
roster is the live org chart; `Router.candidates` is the capable set), lifecycle (spawn → run →
heartbeat → rotate → revoke → reap), work distribution (the durable queue + router), and observability
(the one audit trail). From a handful of agents on one box to thousands across replicas, the *method*
does not change shape — only the store backends swap (the stateless auth path already scales; the
SQLite coordination substrate moves to Postgres/Redis, and the vault to a shared DB/KMS — see
[PLATFORM.md](PLATFORM.md)).

## What must always be a human's decision

The unifying rule of the whole design: **agents propose, request, and execute within a granted envelope;
humans define and change the envelope.** Every item below is a change to the envelope, so every item is
a human decision — enforced as a gate a *service* principal can never satisfy, and every one leaves an
audit record naming the deciding human:

1. **Granting or widening capability** — issuing a role, minting a harness with new scopes, expanding a
   delegation. An agent may request; only a human grants. No agent ever escalates its own privilege.
2. **Approving an irreversible or destructive action** — and the non-author approval itself: the
   approver must be a human principal distinct from the requester.
3. **Onboarding or offboarding an identity** — creating an org, admitting a new agent *type* to
   production, revoking an identity for cause.
4. **Credential and key custody** — rotating an org's vault/KMS key, or consenting to hand a *new*
   downstream service's credentials to the fleet.
5. **Overriding a gate ("break glass")** — always a human action, always audited as a first-class,
   non-suppressible event.

These are the lines the platform draws so that scaling the number of agents never scales the amount of
unaccountable authority. The number of agents can grow without bound; the envelope only ever changes by
a human's hand, and the audit trail always knows whose.
