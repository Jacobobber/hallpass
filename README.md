# hallpass

[![CI](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml/badge.svg)](https://github.com/Jacobobber/hallpass/actions/workflows/ci.yml)

Multi-user auth core for MCP servers: per-user OAuth 2.1 verification against any OIDC provider, an encrypted per-user credential vault, and scope-derived tool gating that is enforced at call time, not just in the catalog. The same identity and scope model also governs agent-to-agent channels, agent orchestration, and relevance-ranked tool search, so one auth layer covers agent-to-tools, agent-to-agent, and finding the right tool among many.

**Status: v1.9 — stable.** Core, MCP adapter, operational layer (audit, rate limiting, availability, idempotency), agent-to-agent channels (with FLEX, a token-efficient message language) and an orchestrator that spawns scoped worker agents and drives them over those channels, tool search, batteries-included setup, a catalog of prewired connectors, a per-provider OAuth connect flow with self-healing token refresh and consent/revoke, transient-error retry with backoff, untrusted-message sanitization, a response-size guard, a `doctor()` config self-check, and a runnable HTTP reference server + `hallpass` CLI are in place and green. The public API (everything exported from `hallpass`) is committed to under semver from 1.0; see [CHANGELOG.md](CHANGELOG.md).

The design essay behind this: [Multi-user is the hard part of an MCP server](docs/multi-user-is-the-hard-part.md).

## Clone and run

`uv` is the only prerequisite. From a fresh clone:

```bash
git clone https://github.com/Jacobobber/hallpass && cd hallpass
uv run python examples/quickstart.py     # a gated, per-user tool server, no setup
uv run hallpass serve --dev              # a live HTTP server + a token + curl to hit it
uv run --group dev pytest -q             # the full suite
```

That runs a working server with no identity provider and no config. The Quick start below is the ~10 lines behind it. More: the connector list is in [docs/CATALOG.md](docs/CATALOG.md), adding one is in [CONTRIBUTING.md](CONTRIBUTING.md), and where this is going is in [docs/IDEAS.md](docs/IDEAS.md).

## Quick start

Define a connector by decorating functions, then get a fully gated server with no identity provider to stand up. About ten lines to a working, per-user, scope-gated tool server:

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

The tool's name comes from the function, its description from the docstring, and its argument schema from the signature. `dev_app` is for local development; for production, `build(...)` wires the same server against your real OIDC provider and a persistent vault key:

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

## Prewired connectors

hallpass ships a catalog of connectors to real services, so you do not have to write one to get started:

```python
from hallpass import catalog, dev_app

app, token = dev_app(connectors=catalog.load_all())   # every catalog connector
# or a subset: connectors=[catalog.load("github"), catalog.load("notion")]

app._vault.store("alice", "github", "ghp_...")          # the user's connected token
app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
```

A connector is a declaration, not code: a base URL, an auth style, and a list of endpoints, each becoming a gated tool that calls the service's REST API with the caller's vaulted credential. That is what makes the catalog cheap to grow toward comprehensive coverage. Today it covers 47 services and 115 tools across dev, productivity, CRM, cloud, payments, observability, and messaging (GitHub, GitLab, Bitbucket, Slack, Discord, Notion, the Google suite, Microsoft Graph, Jira, Confluence, Salesforce, HubSpot, Zendesk, Shopify, Linear, Asana, ClickUp, monday, Todoist, Airtable, Zoom, Box, Figma, Vercel, Cloudflare, DigitalOcean, Sentry, Datadog, PagerDuty, Intercom, Calendly, SendGrid, Postmark, Pipedrive, Stripe, Square, Freshdesk, Spotify, and the LLM APIs); adding another is a ~10-line entry in `catalog.py`. The full list is [docs/CATALOG.md](docs/CATALOG.md).

Requires the `connectors` extra (`pip install 'hallpass[connectors]'`) for the default httpx client; inject any `HttpClient` to use your own. Writing your own connector, one declarative `RestService` or a `ToolKit` of decorated functions, stays as easy as the Quick start shows.

Endpoints send JSON bodies by default; one that sets `form=True` sends `application/x-www-form-urlencoded` instead, which is what unlocks form-only write APIs (Stripe's `stripe_create_customer` / `stripe_update_customer` ship on it). A GraphQL API is a first-class endpoint too: an endpoint with a fixed `graphql=` document becomes a named operation whose tool arguments are the GraphQL variables, so a caller invokes `linear_viewer` rather than hand-writing a query. Services that need more than one credential (Datadog's API key + app key) use the `multi` auth style: the user stores a JSON bundle and each field is placed in its own header or query parameter. The default network client also retries transient failures (see the operational layer) and can cap oversized responses (`catalog.load(name, max_response_bytes=...)`).

## Connecting a user (OAuth)

For the 22 services with a known OAuth flow, hallpass drives the connect end to end so the user's token lands in the vault where the connector reads it. hallpass never touches a browser: `start` returns the authorize URL (with single-use state and PKCE), `finish` exchanges the code and stores the tokens, `refresh` renews them. You supply your OAuth client credentials and wire the two calls to your own redirect routes.

```python
from hallpass import OAuthConnect, catalog

connect = OAuthConnect(
    vault=app._vault,
    providers={"github": catalog.oauth_provider(
        "github", client_id=CID, client_secret=SECRET,
        redirect_uri="https://your-app/oauth/callback",
    )},
)

url = connect.start("alice", "github")     # redirect the user here
# ... provider redirects back to your route with ?state=&code= ...
connect.finish(state, code)                # token stored; catalog connector now works
```

State is single-use and expires, PKCE is used by default, and no token, code, or secret is ever written to a log or an error. The pending-state store is pluggable: the default is in-process, and `SqlitePendingStore(path=...)` shares it across instances so `start` and `finish` can land on different servers behind a load balancer.

One more line makes it self-healing: `connect.attach_refresh(gh)` wires the flow's refresh into the connector, so when a stored token expires and the service answers 401/403, hallpass renews it and retries the call once. The user never sees the expiry. (Or refresh proactively with `connect.valid_token("alice", "github")` before a call.)

Pass a `consent=` ledger and the flow records what each user connected: `connect.consents("alice")` lists their active grants (service, scopes, time), and `connect.disconnect("alice", "github")` revokes one, clearing both the access token and the refresh bundle from the vault. Giving someone your credentials should come with a way to take them back.

## Check your setup

`doctor(app)` inspects a built app and reports what a real deployment usually forgets, without a network call or a real request:

```python
from hallpass import doctor, format_report

print(format_report(doctor(app)))
# [OK  ] tools: 5 tool(s) across 1 connector(s).
# [WARN] no-audit: No audit sink: tool calls and denials are not recorded. ...
# [WARN] ephemeral-vault: Credential vault is in-memory: every connected credential is lost on restart. ...
```

The only error-level finding is `no-tools` (a server with nothing to serve); the rest are warnings a single-process demo can ignore but a production deployment should not.

## The gap

Nearly every public MCP server is single-user: one process, one identity, credentials in env vars. The moment a server fronts real services for a team, three problems appear that the toy pattern cannot answer: who is this request actually from, where do each user's downstream credentials live, and which tools does *this* user get. hallpass is those three answers as a small, transport-agnostic core - a hall pass is a scoped, revocable permission slip issued by an authority and checked by whoever stops you in the corridor, which is exactly what a bearer token is supposed to be.

## The three layers

1. **Identity** (`TokenVerifier`): boring-on-purpose OAuth 2.1 resource-server verification. RS256 against a JWKS, exact issuer and audience, `alg=none` and symmetric algorithms refused, single JWKS refresh on unknown kid then fail closed. The JWKS source is injected: production uses an OIDC provider over HTTPS, tests use a static document, the logic is identical. A `Principal` may be a user or a service (machine-to-machine): configure `TokenVerifier(service_claim=..., service_values=...)` (e.g. Auth0's `gty=client-credentials`, Azure's `idtyp=app`) and a matching token verifies as `principal.is_service`, so an agent acting as itself is distinguishable from one acting for a user — descriptive only; access is still by scopes.
2. **Vault** (`CredentialVault`): downstream service credentials encrypted at rest (Fernet), keyed by (user subject, service). Tool handlers receive a context that can only reach the calling user's credential for that connector's service - cross-user access is unrepresentable at the seam, not forbidden by convention.
3. **Gating** (`ToolGate`): connectors declare required scopes per tool. The catalog is per-principal, and the same check runs again at call time - a client that ignores the menu and calls directly is refused. Deny is the default everywhere.

## Tool search (`search_tools`)

Once a bridge fronts hundreds of tools you cannot list them all into an agent's context; the agent searches for the few it needs. `search_tools(token, query, limit=...)` ranks the caller's tools by relevance and returns the top few. The security property: **gating runs first, the ranker second**, so the ranker only ever sees the caller's authorized tools and search can never surface a tool the caller could not call, no matter how well the query matches it. The query text is never audited (only the hit count), since a query can carry sensitive content.

The default `LexicalRanker` is a zero-dependency BM25 over each tool's name and description, splitting identifier names on camelCase and snake_case so "read a note" matches `read_note`. Swap in an embedding-based ranker by passing any `ToolRanker` to `Hallpass(ranker=...)`; it still only sees the authorized set.

```python
hits = app.search_tools(bearer_token, "send an email", limit=5)  # top authorized matches
```

## The operational layer (optional)

The three layers above decide access. Three more, all off by default and drawn from running a real bridge in production, keep it honest and safe once it has users:

- **Audit** (`AuditSink`): every list and call is recorded - who, what, allowed or denied, and why. Denials are audited too, not just successes, because a refused call is exactly the event a review looks for. Events carry the subject, tool, decision, an opaque reason, and (for a completed call) the handler's `duration_ms`, so the trail doubles as a latency source - no separate metrics dependency. Never a token, claim value, or credential. `InMemoryAuditLog` is the built-in sink; `SqliteAuditLog(path=...)` is a durable one with a `query(subject=..., tool=..., decision=..., since=..., limit=...)` that answers "what did user X do" / "what got denied" / "which calls were slow" after the fact. Pass `audit=` to `Hallpass`.
- **Rate limiting** (`RateLimiter`): per-subject call budgets, so one agent in a loop cannot hammer a downstream on everyone's behalf. `FixedWindowRateLimiter(max_calls, window_seconds)` is a thread-safe sliding window; an over-budget call is refused and audited. Pass `rate_limiter=` to `Hallpass`.
- **Connector availability**: a connector may implement `available() -> bool`; if it reports unavailable at registration (its backend is not configured), its tools are never registered, so an unconfigured connector cannot advertise tools it cannot serve. `unavailable_connectors` reports what was skipped.
- **Transient-error retry** (`RetryingHttpClient` / `RetryPolicy`): the default network client retries 429 and 5xx with exponential backoff, honoring `Retry-After` when the service sends it. 401/403 are deliberately excluded — those are the connector auth-refresh's job, not a blind retry.
- **Circuit breaker** (`CircuitBreakerHttpClient` / `BreakerPolicy`): wrap the client (around the retry client) and, after a run of outages (5xx or connection errors) to a service, the breaker opens and calls fail fast for a cooldown, then a single half-open probe closes or re-opens it. Stops a fleet of agents from hammering a struggling downstream. Client errors (404) are answers, not outages, and never trip it.
- **Response-size guard** (`guard_response`): pass `max_response_bytes=` to `RestConnector` / `catalog.load` and an oversized result becomes an explicit envelope (`hallpass:truncated`, byte counts, a UTF-8-safe preview, and guidance to re-query narrower) instead of silently flooding — or being silently truncated out of — the caller's context.
- **Idempotency** (`IdempotencyStore`): pass `idempotency_key=` to `call_tool` with an injected store and a repeat of the same `(subject, tool, key)` returns the first result instead of running the handler again, so a retried mutation happens at most once. `InMemoryIdempotencyStore` is the built-in TTL default; only successful results are remembered, keys are scoped per subject and tool, and the check runs after authorization.

## Agent-to-agent channels (`A2ABus`)

The other layers bridge an agent to tools; this one bridges agents to each other, using the same identity and scope model. A channel is declared with a `ChannelPolicy` (the scopes a principal needs to post and to read); posting and reading are authorized against the caller's scopes and audited through the same sink. Deny is the default and denial is opaque: an undeclared channel and one you lack scope for fail with the same message, so a caller cannot enumerate channels it may not touch. Delivery is durable and self-contained: an append-only per-channel log, a forward-only ack cursor per (subject, channel), and catch-up on reconnect, so a read without an ack means redelivery, never loss.

```python
from hallpass import A2ABus, ChannelPolicy, Principal

bus = A2ABus(path="team.sqlite3", audit=my_sink)
bus.declare_channel("build", ChannelPolicy(
    post_scopes=frozenset({"build:write"}),
    read_scopes=frozenset({"build:read"}),
))

orchestrator = Principal(subject="orchestrator", scopes=frozenset({"build:write"}))
worker = Principal(subject="worker", scopes=frozenset({"build:read"}))

bus.post(orchestrator, "build", "task: resize batch-7")
for msg in bus.catch_up(worker, "build"):   # inherits anything left unacked
    handle(msg)
    bus.ack(worker, "build", msg.seq)        # ack only after handling
```

A channel body is text one principal wrote and another (often a model) reads, so it is an injection surface. `catch_up` sanitizes bodies on read by default: terminal escape sequences, control characters, Unicode bidi overrides (Trojan-Source), and zero-width/invisible characters are stripped (ZWJ/ZWNJ and emoji are preserved). `frame_untrusted(text)` goes further, wrapping a body in an injection-resistant `<untrusted-message>` boundary before it reaches a model. hallpass neutralizes spoofing and hiding; it does not claim to detect semantic prompt-injection, which is why the frame says "data."

### Live roster (presence)

Who is on a channel right now? An agent heartbeats with `announce`; `roster` lists the subjects seen within a window. Presence is gated by the same scopes as the messages — announcing is a write (post scope), reading the roster is a read (read scope) — and it is soft state: a subject that stops heartbeating simply ages off. Presence is never a grant, so it cannot be used to smuggle access past the scope gate.

```python
bus.announce(worker, "build")                 # on a timer, to stay live
bus.roster(orchestrator, "build")             # -> ["worker"]  (seen in the last 30s)
bus.roster(orchestrator, "build", within=5.0) # tighter liveness window
```

An orchestrator uses it to dispatch only to workers that are actually up, and to notice when one goes dark.

### Direct messages (`open_dm`)

A DM between two agents is not a new transport — it is one channel whose policy is a single scope that only the two parties hold, so privacy is the same scope gate as everything else. `direct_channel(a, b)` derives a stable, order-independent channel name and pair scope; `open_dm(bus, a, b)` declares it and returns the descriptor. You mint each party a token carrying that scope; a third party who learns the channel name still can't post or read it (it lacks the scope, and the bus denies it opaquely).

```python
from hallpass import A2ABus, Principal, open_dm

bus = A2ABus(path="team.sqlite3")
dm = open_dm(bus, "alice", "bob")             # order-independent; idempotent to re-open
alice = Principal("alice", frozenset({dm.scope}))
bob = Principal("bob", frozenset({dm.scope}))

bus.post(alice, dm.name, "bob, can you take batch-7?")
for msg in bus.catch_up(bob, dm.name):
    handle(msg)
```

It is an ordinary channel once opened, so `post`/`catch_up`/`ack` and the live roster all work and stay gated to the pair. Runnable: [`examples/direct_messages.py`](examples/direct_messages.py).

### FLEX: a token-efficient message language

A2A bodies are strings. Free prose is expensive and unreliable to parse; JSON is structured but pays for braces, quotes, and repeated keys on every message. **FLEX** (Fielded Lightweight EXchange) keeps the structure and drops the overhead:

```python
from hallpass import flex

msg = flex.Message(kind="task", to=("alice", "bob"), refs=("PR-42",),
                   fields={"pri": "high", "due": "today"}, note="resize the batch-7 images")
bus.post(orchestrator, "build", flex.encode(msg))
# -> task @alice @bob #PR-42 due=today pri=high | resize the batch-7 images
for m in bus.catch_up(worker, "build"):
    got = flex.parse(m.body)   # got.kind == "task", got.to == ("alice", "bob"), ...
```

Grammar: `<kind> [@recipient]* [#ref]* [key=value]* [ | note]`. For a representative task message that is **70 bytes vs 126 for compact JSON — 44% smaller** (bytes as a tokenizer-agnostic proxy). `parse(encode(m)) == m` round-trips; `parse` is tolerant of hand-written input (unknown tokens fall into the note, nothing dropped) and runs the sanitizer first, since inbound messages are untrusted.

## Orchestrating agents

The channel and FLEX compose into a coordination layer: one agent driving others. The orchestrator and each worker are separate principals on an authorized channel; a task is a FLEX message addressed to a worker and tagged with an id, and the result comes back tagged with the same id.

```python
from hallpass import A2ABus, ChannelPolicy, Orchestrator, Principal, Worker

bus = A2ABus(path="team.sqlite3")
bus.declare_channel("work", ChannelPolicy())

worker = Worker(bus, Principal("resizer", frozenset()), "work")

@worker.handle("resize")
def resize(task):
    return {"status": "done", "width": task.args["width"]}

orch = Orchestrator(bus, Principal("orchestrator", frozenset()), "work")
task_id = orch.dispatch("resizer", "resize", args={"width": "1024"})
worker.run_once()                       # the worker (its own process, in practice) answers
print(orch.gather([task_id])[task_id])  # Result(ok=True, fields={"status": "done", ...})
```

Because it rides `A2ABus`, dispatch and results are gated by the channel's scopes (the harness does not bypass the auth core), durable (a worker that dies mid-task sees it on reconnect), and audited through the same sink. Delivery is at-least-once, so `gather` de-duplicates by task id and handlers should be idempotent; a handler that raises produces a failed result carrying only the exception type, never its message. Addressing is by convention on a shared channel; for hard isolation, give each worker its own channel with its own read scope. A runnable end-to-end demo is [`examples/orchestrator.py`](examples/orchestrator.py).

### Routing by capability

`Router` picks a worker for a task by capability, where capability is the auth scope set. Each worker registers its harness; a task declares the scopes it needs; `route` returns a worker whose harness covers them (round-robin across the eligible ones):

```python
from hallpass import Router

router = Router()
router.register("reviewer",  {"github:read", "github:write"})
router.register("messenger", {"slack:write"})

router.route({"github:write"})   # -> "reviewer"
router.route({"pagerduty:read"}) # -> None: nobody is capable, and that's visible
```

The routing decision uses the same scopes that gate tool calls, so work never lands on an agent that isn't authorized to do it, and an unroutable task surfaces as `None` rather than a silent misroute. Pair it with `dispatch` or the task queue: route first, then hand the task to the chosen worker.

### Spawning agents

The step past dispatching to workers that already exist is *creating* them, each with a different harness and a different task. `Team.spawn` mints each agent a token carrying only its harness scopes and launches it through a pluggable `Spawner`, passing the identity, task, and channel by environment:

```python
from hallpass import Team, SubprocessSpawner, AgentSpec, dev_app

_, mint = dev_app()   # or your IdP's client-credentials flow in production
team = Team(mint=lambda name, scopes: mint(name, scopes),
            spawner=SubprocessSpawner(["python", "agent.py"]), channel="work")

team.spawn(AgentSpec("reviewer",  scopes=frozenset({"github:read"}), task="review PR 42"))
team.spawn(AgentSpec("messenger", scopes=frozenset({"slack:write"}), task="post to #eng"))
```

```python
# inside agent.py (the spawned process)
from hallpass import AgentContext
ctx = AgentContext.from_env()      # ctx.name, ctx.token (scoped), ctx.task, ctx.channel
```

This is the point of building on hallpass rather than cloning agent-teams: **each spawned agent is a scoped identity, not a trusted one**. The reviewer's token carries `github:read` and nothing else, the messenger's carries `slack:write` and nothing else, so a compromised or confused agent can only reach the tools its harness grants, enforced at call time and audited. Isolation between agents is the auth layer. hallpass stays model-agnostic: it provisions the identity and harness and launches the process; what thinks inside it is yours (swap `SubprocessSpawner` for any `Spawner`). A runnable two-process demo is [`examples/spawn_agents.py`](examples/spawn_agents.py).

### Durable task queue

Dispatching over a channel is coordination, not durability. When a fleet of workers pulls from a shared backlog and any of them can die mid-task, you want work to survive a crash and each task to run once. `TaskQueue` is those two properties on SQLite:

```python
from hallpass import TaskQueue

q = TaskQueue(path="work.sqlite3")
q.enqueue("resize", args={"width": "1024"})

task = q.claim("worker-1")             # atomic: no two workers get the same task
# ... do the work ...
q.complete(task.id, worker="worker-1", ok=True, fields={"status": "done"})

q.result(task_id)     # the recorded result, still there after a restart
q.outstanding()       # what a resuming orchestrator still needs
```

`claim` hands one worker exactly one task under a write-locked transaction, so concurrent workers never grab the same one. If the worker that claimed a task dies without completing it, the lease expires and the task becomes claimable again (at-least-once), and `complete` is idempotent by id so a re-run can't overwrite a recorded result. Because it is on disk, a crashed or restarted orchestrator resumes: the backlog and the results are still there. It is a coordination/durability primitive; the auth boundary stays on the tools a worker calls with its scoped token.

### The reference agent loop

hallpass provisions the identity, scope, channel, and queue, but it runs no model loop of its own (that's [deliberate](#what-this-is-not)). What every served agent still needs is the *loop* around its work — claim/receive, run, report, heartbeat, stop — and that loop is the same whichever model (or none) sits in a handler, so it ships once:

```python
from hallpass import serve_queue

# inside the spawned agent's process:
serve_queue(
    q, "worker-1", {"resize": lambda task: {"width": task.args["width"]}},
    heartbeat=lambda: bus.announce(me, "fleet"),   # stay on the live roster
    stop=should_stop,                              # or omit to drain-and-return
)
```

`serve_queue` claims from a `TaskQueue`, dispatches by operation, and completes each task (idempotent, lease-safe); a raising handler completes `ok=False` with only the exception *type*, never its message. `run_worker` is the same loop over an A2A `Worker`/channel. Both take an injectable `sleep` (so they test without wall-clock waits) and a `heartbeat` hook that ties in the live roster. The handler is where your model or tool call goes — the auth boundary is unchanged, since it acts with the agent's scoped token. Runnable: [`examples/reference_agent.py`](examples/reference_agent.py).

## Assembling it by hand

`build()` and `ToolKit` are conveniences over the same core; if you want to wire the layers yourself (or write a connector as a class), it looks like this:

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

A connector is a class with a `service` name and a `tools()` method returning `ToolSpec`s; handlers get a `UserContext` with the caller's identity and that user's credential for the connector's service. For a complete, self-contained program you can run right now (`python examples/minimal.py`, core install only) that mints its own token and shows per-user gating end to end, see [`examples/minimal.py`](examples/minimal.py); [`tests/test_end_to_end.py`](tests/test_end_to_end.py) is the same idea as a test.

## Run it as a server (CLI)

Installing hallpass puts a `hallpass` command on your path. It's a thin shell over the library, so it can't drift from it:

```bash
hallpass serve --dev            # a live multi-user server on localhost + a ready demo token + curl
hallpass doctor --dev           # config self-check (exits non-zero on an error)
hallpass catalog list           # every connector and its tool count
hallpass catalog search "list my github repos"   # rank catalog tools by a query
```

`hallpass serve --dev` stands up a real HTTP server (stdlib only, no framework) with the full connector catalog and prints a self-signed token plus the curl to hit it:

```bash
curl http://127.0.0.1:8000/healthz
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/tools          # the caller's gated catalog
curl -H "Authorization: Bearer $TOK" -d '{"arguments":{}}' \
     http://127.0.0.1:8000/call/github_list_my_repos                      # a gated call
```

The endpoints route straight through the core: `/tools` returns the catalog *for that bearer*, `/call/<tool>` verifies and gates before running, an invalid token is `401`, and an ungranted tool is the same `404` as a nonexistent one — no leak of what exists. For production, `hallpass serve` reads `HALLPASS_ISSUER` / `HALLPASS_AUDIENCE` / `HALLPASS_JWKS_URL` (+ `HALLPASS_VAULT_KEY`) from the environment. The server is `hallpass.http_server` (a pure `handle_request` + a stdlib `http.server` wrapper); it's a reference/dev transport — put TLS and rate limiting at a proxy, or use the MCP adapter below.

## Serving it over MCP

The core is transport-agnostic; `hallpass[mcp]` adds a thin adapter that wires it into an MCP low-level server. You supply a token provider that reads the validated bearer from your transport's auth context (the ASGI scope, under streamable HTTP):

```python
from hallpass.mcp_adapter import build_mcp_server

async def token_provider() -> str:
    return current_request_bearer()  # from your transport's auth context

server = build_mcp_server(app, token_provider)  # hand to any MCP transport
```

Every list and call the server answers is gated by the core against that token. An unauthenticated caller gets an empty catalog and a refused call; an ungranted tool is indistinguishable from one that does not exist. A `ToolSpec` may carry an `input_schema` (JSON Schema), which the adapter advertises so clients validate arguments; tools that omit it advertise an open object. It also carries `ToolAnnotations` (read-only / destructive / idempotent hints, auto-derived from the HTTP verb for catalog connectors — GET is read-only, DELETE destructive, PUT idempotent), which the adapter maps to MCP's tool-annotation hints and the HTTP server includes in `/tools`, so a client can warn before a destructive call. The hints are advisory; access is still decided by scopes. See `tests/test_mcp_adapter.py` and `tests/test_annotations.py`.

## Install

```bash
pip install git+https://github.com/Jacobobber/hallpass        # core
pip install "hallpass[mcp] @ git+https://github.com/Jacobobber/hallpass"  # + MCP adapter
```

Python 3.10+. One runtime dependency for the core (`pyjwt[crypto]`); the MCP adapter and the HTTP JWKS fetcher are optional extras. CI covers 3.10 through 3.14 (all five) on Linux and Windows, with ruff and `mypy --strict`.

## Performance

hallpass stays out of the way on the hot path. Token verification caches a verified principal until the token's own expiry, so a repeated bearer skips the RSA signature check (a token verifies identically until it expires, and JWT verification consults no revocation list, so the cache changes nothing observable). The default HTTP client pools connections, so repeated calls to a service reuse a kept-alive TLS connection instead of a fresh handshake each time. Measured with `python evals/benchmark.py`:

| operation | ops/sec |
|---|---|
| token verify, cached | ~3,500,000 |
| token verify, uncached (RSA) | ~22,000 |
| call-time gating | ~4,700,000 |
| FLEX encode / parse | ~630,000 / ~285,000 |

A repeated token is roughly 160x cheaper than a cold RSA verify, and the pooled HTTP client is ~5x an unpooled one even on loopback (the gap widens over real TLS, where the handshake dominates). Numbers are ops/sec on the author's machine, for relative comparison. Async/uvloop is a deliberate non-goal for now: the core is synchronous, and the per-call CPU cost is already small enough that connection reuse and the verify cache are where the real time was.

## The security suite

Every test names a way multi-user tool servers get broken and proves this design refuses it: wrong-audience tokens (the confused deputy), `alg=none` and HS256 downgrade, signatures from the wrong key, unknown-kid fail-closed with single-refresh rotation, secrets in the database file, cross-user and cross-service vault isolation, wrong-key decryption, call-time gating bypass, partial-scope unlock, and cross-layer leaks (user B's call can never surface user A's credential).

Beyond the named cases, `tests/test_properties.py` puts the four core invariants under a property-based fuzzer (Hypothesis): across generated scope sets, subjects, and queries, gating holds iff the caller has the scope, no subject can read another's vaulted credential, tool search never exceeds the authorized set, and A2A reads require the read scope. A counterexample prints the exact input that broke it.

```bash
uv run --group dev pytest -q
```

Tool search is also measured, not asserted on faith: `python evals/tool_search_benchmark.py` scores the ranker against a naive keyword-overlap baseline on labelled queries and prints the numbers (currently MRR 0.94 vs 0.81, top-3 on every query). If a ranking change ever makes search no better than keyword matching, the harness says so and the regression guard fails.

## What this is not

Not an identity provider (bring any OIDC issuer), not an MCP framework (the core is transport-agnostic; the MCP adapter is a thin optional extra), and not a gateway in front of servers - it is the inside of one server done right.

## Contributing

Issues and questions welcome. This is a reference implementation of an auth boundary, so the bar for changes is high: correctness, portability, and security fixes are welcome, and anything touching the verifier, vault, or gate needs a test naming the property it protects. Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT
