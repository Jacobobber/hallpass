"""A dependency-free HTTP reference server for a Hallpass core.

The point is to make "clone and run a real multi-user server" true, with only
the standard library: no framework, no extra install. It exposes the core's two
operations over HTTP with bearer auth, so the per-user gating can be exercised
with `curl` instead of a test harness. The MCP adapter (``hallpass.mcp_adapter``)
is the path for real MCP transport; this is the plain-HTTP demonstrator.

    GET  /healthz            -> {"status": "ok"}                 (no auth; liveness)
    GET  /readyz             -> {"status": "ready"}             (no auth; readiness, 503 if not)
    GET  /tools              -> {"tools": [...]}                 (bearer -> the caller's catalog)
    POST /call/<tool>        -> {"result": ...}                  (bearer; body: {"arguments": {...}})

``/healthz`` is liveness (the process answers); ``/readyz`` is readiness (its
backends answer) and returns 503 when a dependency is unreachable, so a load
balancer drains a replica whose database is down instead of routing to it. The
readiness body is opaque status only (``/readyz`` is unauthenticated, so it must
not reveal which backends exist or which is degraded).

The request handling is a pure function (``handle_request``) so it is tested
without opening a socket; ``serve`` is the thin ``http.server`` wrapper around
it. Errors never carry the bearer or a credential, and an ungranted tool is
indistinguishable from a nonexistent one (same 404), matching the core.

NOT hardened for production: no TLS, a fixed 1 MiB body cap (not tuned), and no
per-IP rate limiting. Terminate TLS and rate-limit at a proxy, or use this as
the reference for your own transport.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import Hallpass
from .gating import UnknownTool
from .identity import VerificationError
from .ratelimit import RateLimited
from .rest import ConnectorError

__all__ = ["handle_request", "serve", "bearer_from_header"]

_CALL_PREFIX = "/call/"
# Cap the request body so an unauthenticated caller cannot stream unbounded
# bytes (the read happens before auth). Generous for JSON tool arguments.
_MAX_BODY_BYTES = 1 << 20  # 1 MiB


def bearer_from_header(authorization: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header;
    return "" when absent or malformed (the core treats "" as unauthenticated)."""
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def handle_request(
    app: Hallpass,
    method: str,
    path: str,
    *,
    bearer: str,
    body: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    """Map one request to ``(status_code, json_body)``. Pure: no I/O, so the
    routing and error mapping are testable without a server. No response ever
    contains the bearer or a credential."""
    if method == "GET" and path == "/healthz":
        return 200, {"status": "ok"}

    if method == "GET" and path == "/readyz":
        ready, _checks = app.check_readiness()
        # 503 so a load balancer stops routing to a replica that cannot serve.
        # The body is opaque status only: /readyz is unauthenticated, so it must
        # not reveal which backends exist or which is degraded (that topology is
        # for the logs/audit and a later authenticated admin endpoint).
        return (200 if ready else 503), {"status": "ready" if ready else "not ready"}

    if method == "GET" and path == "/tools":
        try:
            specs = app.list_tools(bearer)
        except VerificationError:
            return 401, {"error": "authentication required"}
        return 200, {
            "tools": [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": s.input_schema,
                    "annotations": {
                        "read_only": s.annotations.read_only,
                        "destructive": s.annotations.destructive,
                        "idempotent": s.annotations.idempotent,
                    },
                }
                for s in specs
            ]
        }

    if method == "POST" and path.startswith(_CALL_PREFIX):
        name = path[len(_CALL_PREFIX) :]
        arguments = {}
        idempotency_key = None
        if isinstance(body, dict):
            raw_args = body.get("arguments", {})
            arguments = raw_args if isinstance(raw_args, dict) else {}
            key = body.get("idempotency_key")
            idempotency_key = key if isinstance(key, str) else None
        try:
            result = app.call_tool(
                bearer, name, arguments, idempotency_key=idempotency_key
            )
            # Commit to 200 only if the result actually serializes, so a handler
            # returning a non-JSON object fails here (opaque 500) instead of
            # str()-leaking its internals into the body.
            json.dumps(result)
        except VerificationError:
            return 401, {"error": "authentication required"}
        except UnknownTool:
            # ToolDenied subclasses UnknownTool; ungranted and nonexistent are
            # deliberately the same opaque 404 so the catalog cannot be mapped.
            return 404, {"error": "unknown tool or not permitted"}
        except RateLimited:
            return 429, {"error": "rate limited"}
        except ConnectorError as exc:
            # Opaque: the detailed message names the backend host and echoes the
            # caller's path args; keep it out of the client body (audit has it).
            return 502, {"error": "upstream service error", "status": exc.status}
        except Exception:  # noqa: BLE001 - HTTP boundary: never leak a traceback
            # A tool handler raised (or returned) something unexpected; return an
            # opaque 500 rather than let the detail or a stack trace reach out.
            return 500, {"error": "internal error"}
        return 200, {"result": result}

    return 404, {"error": "not found"}


def serve(
    app: Hallpass, *, host: str = "127.0.0.1", port: int = 8000
) -> ThreadingHTTPServer:
    """Start (and return) a threaded HTTP server bound to ``host:port`` that
    routes through ``handle_request``. Call ``serve_forever()`` on the result,
    or ``shutdown()`` to stop. Bind to localhost by default; put a TLS proxy in
    front for anything beyond a local demo."""

    class _Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            # No default= serializer: payloads here are only the small, known
            # response dicts (handle_request validated any tool result already).
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            bearer = bearer_from_header(self.headers.get("Authorization"))
            body: dict[str, Any] | None = None
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._respond(400, {"error": "invalid Content-Length"})
                return
            # Cap the body BEFORE reading it: auth happens downstream, so an
            # unbounded read would be a pre-auth memory DoS.
            if length > _MAX_BODY_BYTES:
                self._respond(413, {"error": "request body too large"})
                return
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._respond(400, {"error": "invalid JSON body"})
                    return
            status, payload = handle_request(
                app, method, self.path, bearer=bearer, body=body
            )
            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802  (http.server naming)
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *args: Any) -> None:
            # Silence the default stderr access log; wire the audit sink for a
            # real record. (Prevents the bearer-bearing request line from ever
            # hitting stderr.)
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    # Non-daemon worker threads: on shutdown, the server joins in-flight request
    # threads instead of the process exiting out from under them. A container or
    # k8s stop (SIGTERM) must let a request drain, not cut it mid-flight.
    server.daemon_threads = False
    return server
