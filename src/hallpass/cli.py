"""The ``hallpass`` command line: run a server, check config, browse the catalog.

Installed as a console script (``hallpass ...``). Everything here is a thin
shell over the library so the CLI can't drift from it:

    hallpass serve --dev            # a live demo server + a ready token + curl
    hallpass serve                  # production: configured from env (see below)
    hallpass migrate                # provision the Postgres schema once (init Job)
    hallpass doctor [--dev]         # config self-check; exits non-zero on an error
    hallpass catalog list           # every connector and its tool count
    hallpass catalog search "..."   # rank catalog tools by a query

Production `serve`/`doctor` read config from the environment:
    HALLPASS_ISSUER, HALLPASS_AUDIENCE, HALLPASS_JWKS_URL   (required)
    HALLPASS_VAULT_KEY                                      (recommended; else ephemeral)
    HALLPASS_DATABASE_URL                                   (optional; Postgres -> shared vault)
    HALLPASS_REDIS_URL                                      (optional; Redis -> shared idempotency + rate limit)
    HALLPASS_RATE_LIMIT                                     (optional; "max/window_seconds", e.g. "120/60")
    HALLPASS_AUDIT_PATH                                     (optional; SQLite audit file when no DATABASE_URL)
    HALLPASS_HOST, HALLPASS_PORT                            (optional)

Set HALLPASS_DATABASE_URL and HALLPASS_REDIS_URL to run multiple replicas
behind a load balancer with one shared vault, idempotency cache, and
rate-limit budget; with neither, a single node runs on local SQLite.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Sequence
from typing import Any

from . import catalog as catalog_mod
from .audit import AuditSink, SqliteAuditLog
from .control import ControlPlane
from .core import Hallpass
from .diagnostics import doctor, format_report
from .humangate import InMemoryHumanGateLedger
from .identity import InMemoryRevocationList
from .search import LexicalRanker
from .server import build, dev_app
from .taskqueue import TaskQueue


def _parse_rate_limit(raw: str | None) -> tuple[int, float] | None:
    """Parse HALLPASS_RATE_LIMIT ("max/window_seconds", e.g. "120/60") into the
    ``(max_calls, window_seconds)`` build() expects. Raises SystemExit with an
    actionable message on a malformed value so a typo fails fast, not silently."""
    if not raw:
        return None
    try:
        calls_s, window_s = raw.split("/", 1)
        calls, window = int(calls_s), float(window_s)
        if calls < 1 or window <= 0:
            raise ValueError
    except ValueError:
        raise SystemExit(
            f"invalid HALLPASS_RATE_LIMIT {raw!r}: expected 'max/window_seconds'"
            " with max>=1 and window>0, e.g. '120/60'"
        ) from None
    return calls, window


def _app_from_env() -> tuple[Hallpass, ControlPlane]:
    issuer = os.environ.get("HALLPASS_ISSUER")
    audience = os.environ.get("HALLPASS_AUDIENCE")
    jwks_url = os.environ.get("HALLPASS_JWKS_URL")
    missing = [
        n
        for n, v in (
            ("HALLPASS_ISSUER", issuer),
            ("HALLPASS_AUDIENCE", audience),
            ("HALLPASS_JWKS_URL", jwks_url),
        )
        if not v
    ]
    if missing:
        raise SystemExit(
            "missing required env: " + ", ".join(missing) + " (or pass --dev)"
        )
    assert issuer and audience and jwks_url
    # A token is a service (machine) principal when it carries this claim=value;
    # without it every token reads as human and the human-gate's service refusal
    # never fires (a machine token could clear a gate meant for a person).
    service_claim = os.environ.get("HALLPASS_SERVICE_CLAIM") or None
    service_values = frozenset(
        os.environ.get("HALLPASS_SERVICE_VALUES", "").replace(",", " ").split()
    )
    if service_claim and not service_values:
        raise SystemExit(
            "HALLPASS_SERVICE_CLAIM is set but HALLPASS_SERVICE_VALUES is empty:"
            " no token would ever be recognized as a service principal, so the"
            " human-gate's service refusal is silently disabled. Set the value(s)"
            " (e.g. 'client-credentials') or unset the claim."
        )
    database_url = os.environ.get("HALLPASS_DATABASE_URL")
    audit = _audit_from_env(database_url)
    # The revocation list is shared between the verifier (which consults it on
    # every verify) and the control plane (which mutates it), so a revoke takes
    # effect on this server's own tokens too.
    revocations, queue, gates = _control_subsystems(database_url)
    app = build(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        vault_key=os.environ.get("HALLPASS_VAULT_KEY"),
        database_url=database_url,
        redis_url=os.environ.get("HALLPASS_REDIS_URL"),
        rate_limit=_parse_rate_limit(os.environ.get("HALLPASS_RATE_LIMIT")),
        service_claim=service_claim,
        service_values=service_values,
        audit=audit,
        revocations=revocations,
        connectors=catalog_mod.load_all(),
    )
    control = ControlPlane(
        verifier=app.verifier,
        audit=audit,
        queue=queue,
        revocations=revocations,
        gates=gates,
    )
    return app, control


def _control_subsystems(
    database_url: str | None,
) -> tuple[Any, TaskQueue, Any]:
    """The subsystems the control plane observes and manages. With a database
    they are the SHARED Postgres backends (so an admin action on any replica is
    fleet-wide, and the audit tail / gates / revocations are one record); on a
    single node they are in-process. Returns ``(revocations, queue, gates)``."""
    if database_url:
        from .postgres_backends import (
            PostgresHumanGateLedger,
            PostgresRevocationList,
            PostgresTaskQueueBackend,
        )
        from .revocation import CachedRevocationList

        return (
            CachedRevocationList(PostgresRevocationList(database_url)),
            TaskQueue(backend=PostgresTaskQueueBackend(database_url)),
            PostgresHumanGateLedger(database_url),
        )
    return InMemoryRevocationList(), TaskQueue(), InMemoryHumanGateLedger()


def _audit_from_env(database_url: str | None) -> AuditSink | None:
    """Pick a durable audit sink so a production ``serve`` records its decisions
    (it wired none before): shared Postgres when ``database_url`` is set, so the
    authorization trail is one central record across replicas rather than per-pod
    files lost on restart; else a local SQLite file if ``HALLPASS_AUDIT_PATH`` is
    set; else none."""
    if database_url:
        from .postgres_backends import PostgresAuditLog

        return PostgresAuditLog(database_url)
    path = os.environ.get("HALLPASS_AUDIT_PATH")
    if path:
        return SqliteAuditLog(path=path)
    return None


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.dev:
        app = dev_app(connectors=catalog_mod.load_all())[0]
    else:
        app = _app_from_env()[0]
    findings = doctor(app)
    print(format_report(findings))
    app.close()
    # non-zero exit if anything is an error, so `hallpass doctor` works in CI
    return 1 if any(f.level == "error" for f in findings) else 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Provision the Postgres schema once (an init container / migration Job),
    so replicas boot against tables that already exist instead of racing each
    other's CREATE on a 0->N scale-up."""
    dsn = os.environ.get("HALLPASS_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "hallpass migrate needs HALLPASS_DATABASE_URL (the Postgres a"
            " multi-replica deployment shares). Nothing to migrate on SQLite."
        )
    from .postgres_backends import migrate

    version = migrate(dsn)
    print(f"hallpass schema provisioned at version {version}")
    return 0


def _cmd_catalog(args: argparse.Namespace) -> int:
    if args.action == "list":
        for name in catalog_mod.names():
            svc = catalog_mod.SERVICES[name]
            tenant = " (per-tenant)" if svc.requires_base_url else ""
            oauth = " oauth" if name in catalog_mod.OAUTH else ""
            print(f"{name:20s} {len(svc.endpoints):3d} tools{tenant}{oauth}")
        return 0
    # search
    query = " ".join(args.query)
    tools = []
    for name in catalog_mod.names():
        base = "https://tenant.example" if catalog_mod.requires_base_url(name) else None
        tools.extend(catalog_mod.load(name, base_url=base).tools())
    ranked = LexicalRanker().rank(query, tools)[: args.limit]
    for spec in ranked:
        print(f"{spec.name:32s} {spec.description}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .http_server import serve  # deferred: only needed for this command

    host = args.host or os.environ.get("HALLPASS_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("HALLPASS_PORT", "8000"))
    if args.dev and (
        os.environ.get("HALLPASS_DATABASE_URL")
        or host not in ("127.0.0.1", "localhost", "::1")
    ):
        # --dev wires a self-signed minter that will sign a token for ANY
        # subject and scopes; reachable in production it makes every token
        # (including admin scopes) forgeable. Refuse when a production signal
        # is present: a shared database, or a bind address other than loopback.
        raise SystemExit(
            "refusing 'serve --dev' with a production signal present"
            f" (HALLPASS_DATABASE_URL set or non-loopback host {host!r}):"
            " the dev minter forges tokens for any subject. Run without --dev."
        )
    control: ControlPlane | None = None
    if args.dev:
        app, token = dev_app(connectors=catalog_mod.load_all())
        demo = token("demo-user", ["github:read"])
        print(f"hallpass dev server on http://{host}:{port}  (NOT for production)")
        print("\na ready demo token (dev key, self-signed):\n")
        print(f"  export TOK={demo}\n")
        print("try it:")
        print(f"  curl http://{host}:{port}/healthz")
        print(f"  curl http://{host}:{port}/readyz")
        print(f'  curl -H "Authorization: Bearer $TOK" http://{host}:{port}/tools')
    else:
        app, control = _app_from_env()
        # Report which backends are active (never the URLs/secrets) so an
        # operator can confirm a multi-replica rollout is actually shared.
        vault = "postgres" if os.environ.get("HALLPASS_DATABASE_URL") else "sqlite"
        shared = "redis" if os.environ.get("HALLPASS_REDIS_URL") else "in-process"
        print(f"hallpass on http://{host}:{port}  (vault: {vault}, shared: {shared})")
        print(f"  admin dashboard + gated /admin API at http://{host}:{port}/admin")
    server = serve(app, host=host, port=port, control=control)
    _serve_until_signal(server, app)
    return 0


def _serve_until_signal(
    server: Any,
    app: Hallpass,
    *,
    install_signals: bool = True,
    stop: threading.Event | None = None,
) -> None:
    """Run the server until SIGINT or SIGTERM, then shut down gracefully.

    Containers and k8s stop a process with SIGTERM, not SIGINT, so both must
    trigger the same clean path: stop accepting, let in-flight requests drain
    (the server runs non-daemon worker threads), then release the app's
    resources. The accept loop runs on a background thread so the signal,
    delivered to the main thread, sets the stop event without racing it."""
    stop = stop or threading.Event()

    def _graceful(_signum: int, _frame: object) -> None:
        stop.set()

    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _graceful)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        stop.wait()
    finally:
        print("\nshutting down")
        server.shutdown()  # stop accepting; joins draining worker threads
        server.server_close()
        app.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hallpass", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the HTTP reference server")
    p_serve.add_argument(
        "--dev", action="store_true", help="self-signed dev app + demo token"
    )
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_doctor = sub.add_parser(
        "doctor", help="config self-check (exits non-zero on error)"
    )
    p_doctor.add_argument(
        "--dev", action="store_true", help="check a self-signed dev app"
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_migrate = sub.add_parser(
        "migrate", help="provision the Postgres schema (run once before scaling)"
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_cat = sub.add_parser("catalog", help="browse the connector catalog")
    cat_sub = p_cat.add_subparsers(dest="action", required=True)
    cat_sub.add_parser("list", help="list every connector and its tool count")
    p_search = cat_sub.add_parser("search", help="rank catalog tools by a query")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--limit", type=int, default=10)
    p_cat.set_defaults(func=_cmd_catalog)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
