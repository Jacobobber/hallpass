# Deploying hallpass

hallpass runs two ways from the same image: a **single node** on SQLite, or a
**multi-replica** fleet behind a load balancer on shared Postgres + Redis. Both
are `hallpass serve`, configured entirely by environment variables; nothing is
compiled in.

## TL;DR — multi-replica in one command

```sh
cp env.example .env      # your issuer / audience / JWKS URL + a generated vault key
docker compose up --build
curl localhost:8080/healthz     # {"status":"ok"}
curl localhost:8080/readyz      # {"status":"ready"}  (503 if a backend is down)
```

That brings up Postgres, Redis, a one-shot schema migration, two app replicas,
and a round-robin load balancer on `:8080`. See [`docker-compose.yml`](../docker-compose.yml).

## Configuration (environment)

| Variable | Required | Purpose |
|---|---|---|
| `HALLPASS_ISSUER` | yes | OIDC issuer the bearer must match |
| `HALLPASS_AUDIENCE` | yes | audience the bearer must match |
| `HALLPASS_JWKS_URL` | yes | your provider's JWKS endpoint (verification keys) |
| `HALLPASS_VAULT_KEY` | prod | Fernet key for the credential vault — **stable + secret** |
| `HALLPASS_DATABASE_URL` | multi-replica | Postgres → shared vault + audit + queue + A2A + channel policies |
| `HALLPASS_REDIS_URL` | multi-replica | Redis → shared idempotency + rate-limit budget |
| `HALLPASS_RATE_LIMIT` | optional | `"max/window_seconds"`, e.g. `120/60` |
| `HALLPASS_AUDIT_PATH` | optional | SQLite audit file when there is no `DATABASE_URL` |
| `HALLPASS_SERVICE_CLAIM` / `_VALUES` | optional | mark machine tokens as service principals (see below) |
| `HALLPASS_HOST` / `HALLPASS_PORT` | optional | bind address (the image sets `0.0.0.0:8000`) |

`hallpass serve` prints the active backend mode at startup (`vault: postgres,
shared: redis`) — never the URLs. `hallpass doctor` validates the same config
without starting the server.

## Single node vs. multi-replica

**Single node.** No `DATABASE_URL`/`REDIS_URL`: SQLite vault, in-process
idempotency and rate limiting. Set `HALLPASS_VAULT_KEY` so credentials survive a
restart, and `HALLPASS_AUDIT_PATH` for a durable audit trail. Correct and simple;
it does not scale past one process.

**Multi-replica.** Set `DATABASE_URL` **and** `REDIS_URL`. The stateless auth
path (verify → gate → vault-read → connector) scales horizontally, so the load
balancer is **plain round-robin — no sticky sessions**. Every store is networked,
so the replicas share one vault, audit trail, idempotency cache, and rate-limit
budget. Setting `DATABASE_URL` without `REDIS_URL` warns: the vault is shared but
idempotency/rate-limit stay per-process (a retry on another replica re-runs the
mutation; the per-subject budget becomes N× the cap).

Run the schema migration once before scaling — an init container or a Job:

```sh
HALLPASS_DATABASE_URL=… hallpass migrate
```

Backends also create their tables race-free on first construction (DDL runs
under a Postgres advisory lock), so a scale-up cannot crash on concurrent
`CREATE`; `migrate` is the explicit, ordered path and records a schema version.

## Health checks

- **`GET /healthz`** — liveness. Static process-up check, touches no backend.
  Point a container `livenessProbe` here; a database blip must not restart
  healthy pods.
- **`GET /readyz`** — readiness. Real round-trip to the vault backend (and the
  idempotency store if wired); returns **503** when a dependency is unreachable
  so the load balancer drains this replica. Point a `readinessProbe` here. The
  body is opaque (`{"status":"ready"}`) — it deliberately does not name which
  backend is degraded. It does **not** probe the IdP/JWKS: an IdP blip must not
  drain the whole fleet, and `HttpJwks` serves the last good keys through one.

Kubernetes probes:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
```

## Security posture

- **TLS terminates at the load balancer.** The app speaks plain HTTP behind the
  proxy by design; publish only the LB, keep the app replicas on the internal
  network so bearer tokens never cross the host in plaintext.
- **Forward only the client `Authorization` header.** Never inject a trusted
  identity header (`X-Forwarded-User` and the like) — the app verifies the
  bearer in-process on every request, so there is exactly one auth path.
- **Secrets at runtime only.** `HALLPASS_VAULT_KEY` and the DSNs arrive as env /
  mounted secrets; never bake them into an image layer. The startup banner,
  logs, and error bodies never print a token, DSN, or credential.
- **Never run `serve --dev` in production.** The dev app mints a token for any
  subject and scope; `serve --dev` refuses to start when a production signal is
  present (a shared database, or a non-loopback bind host).
- **Service vs. human tokens.** If you use human gates, set
  `HALLPASS_SERVICE_CLAIM`/`HALLPASS_SERVICE_VALUES` so machine tokens are
  recognized as service principals — otherwise every token reads as human and a
  machine token could clear a gate meant for a person.

## Control plane (admin + observability)

Opt-in. Construct a `ControlPlane` over the same verifier and pass it to the
server; it exposes gated `/admin/*` endpoints and an admin dashboard at
`GET /admin`:

```python
from hallpass import ControlPlane
from hallpass.http_server import serve

control = ControlPlane(verifier=app.verifier, audit=audit, queue=queue,
                       revocations=revocations, gates=gates)
serve(app, host="0.0.0.0", port=8000, control=control)
```

Every admin call is the same verify → admin-scope (`admin:queue` / `admin:audit`
/ `admin:revoke` / `admin:gate`) → action → audit path; a non-admin gets the same
opaque `404` as any unknown path, so the surface can't be probed. The dashboard
holds no privilege of its own — it's a static shell that calls the gated API with
the operator's pasted bearer.

**Multi-replica caveat:** `InMemoryRevocationList` is per-process, so a revoke via
one replica's control plane only affects that replica. For fleet-wide revocation,
back the `RevocationList` with a shared store (a short-TTL cache over Postgres) so
`is_revoked` stays fast on the verify hot path — a planned backend. The audit tail
already reads the shared store, so it shows the whole fleet.

## Operational notes

- **Connections.** The Postgres backends open a connection per operation. Under
  many replicas, put **PgBouncer in transaction pooling mode** in front of
  Postgres (statement mode breaks the A2A append's advisory-lock-then-insert
  transaction). A built-in pool is a planned option.
- **Rate limiting** is a fixed window, so a burst straddling a boundary can
  reach up to 2× the cap within one window; the shared budget is the point.
  Put per-IP connection/rate limits for the pre-auth paths (`/healthz`,
  `/readyz`, the body read) at the load balancer.
- **Connector availability is read once, at startup.** A connector whose backend
  is down when a replica boots serves none of that connector's tools for the
  life of that replica, and replicas can end up with different catalogs. Restart
  the replica to recover.
- **Pin the base image by digest** in your own registry for reproducible builds;
  the public `Dockerfile` pins a patch tag so the example builds anywhere.
