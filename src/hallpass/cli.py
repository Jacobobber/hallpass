"""The ``hallpass`` command line: run a server, check config, browse the catalog.

Installed as a console script (``hallpass ...``). Everything here is a thin
shell over the library so the CLI can't drift from it:

    hallpass serve --dev            # a live demo server + a ready token + curl
    hallpass serve                  # production: configured from env (see below)
    hallpass doctor [--dev]         # config self-check; exits non-zero on an error
    hallpass catalog list           # every connector and its tool count
    hallpass catalog search "..."   # rank catalog tools by a query

Production `serve`/`doctor` read config from the environment:
    HALLPASS_ISSUER, HALLPASS_AUDIENCE, HALLPASS_JWKS_URL   (required)
    HALLPASS_VAULT_KEY                                      (recommended; else ephemeral)
    HALLPASS_HOST, HALLPASS_PORT                            (optional)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import catalog as catalog_mod
from .core import Hallpass
from .diagnostics import doctor, format_report
from .search import LexicalRanker
from .server import build, dev_app


def _app_from_env() -> Hallpass:
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
    return build(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        vault_key=os.environ.get("HALLPASS_VAULT_KEY"),
        connectors=catalog_mod.load_all(),
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    app = dev_app(connectors=catalog_mod.load_all())[0] if args.dev else _app_from_env()
    findings = doctor(app)
    print(format_report(findings))
    app.close()
    # non-zero exit if anything is an error, so `hallpass doctor` works in CI
    return 1 if any(f.level == "error" for f in findings) else 0


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
    if args.dev:
        app, token = dev_app(connectors=catalog_mod.load_all())
        demo = token("demo-user", ["github:read"])
        print(f"hallpass dev server on http://{host}:{port}  (NOT for production)")
        print("\na ready demo token (dev key, self-signed):\n")
        print(f"  export TOK={demo}\n")
        print("try it:")
        print(f"  curl http://{host}:{port}/healthz")
        print(f'  curl -H "Authorization: Bearer $TOK" http://{host}:{port}/tools')
    else:
        app = _app_from_env()
        print(f"hallpass on http://{host}:{port}")
    server = serve(app, host=host, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.shutdown()
        app.close()
    return 0


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
