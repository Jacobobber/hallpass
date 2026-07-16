"""The /admin/* HTTP surface routes to the control plane with the same gating,
and a non-admin sees the same opaque 404 as any unknown path -- the admin surface
cannot be probed over HTTP. handle_request is pure, so this needs no socket."""

from hallpass import (
    AdminScopes,
    ControlPlane,
    InMemoryHumanGateLedger,
    InMemoryRevocationList,
    SqliteAuditLog,
    TaskQueue,
    dev_app,
)
from hallpass.http_server import handle_request


def _rig():
    app, token = dev_app()
    audit = SqliteAuditLog()
    queue = TaskQueue()
    rev = InMemoryRevocationList()
    gates = InMemoryHumanGateLedger()
    cp = ControlPlane(
        verifier=app.verifier, audit=audit, queue=queue, revocations=rev, gates=gates
    )
    return app, token, cp, rev, gates


def test_admin_queue_requires_scope_over_http():
    app, token, cp, _rev, _gates = _rig()
    admin = token("ops", [AdminScopes.QUEUE])
    status, payload = handle_request(
        app, "GET", "/admin/queue", bearer=admin, body=None, control=cp
    )
    assert status == 200 and "queue" in payload
    # a non-admin gets the SAME opaque 404 as any unknown path
    status2, payload2 = handle_request(
        app, "GET", "/admin/queue", bearer=token("nobody", []), body=None, control=cp
    )
    assert status2 == 404 and payload2 == {"error": "not found"}
    app.close()


def test_admin_surface_is_invisible_without_control():
    """No control plane wired -> /admin/* is just an unknown path (404), so the
    surface does not even exist to probe."""
    app, token, _cp, _rev, _gates = _rig()
    status, _ = handle_request(
        app, "GET", "/admin/queue", bearer=token("ops", [AdminScopes.QUEUE]), body=None
    )
    assert status == 404
    app.close()


def test_admin_revoke_over_http():
    app, token, cp, rev, _gates = _rig()
    admin = token("ops", [AdminScopes.REVOKE])
    status, payload = handle_request(
        app,
        "POST",
        "/admin/revoke",
        bearer=admin,
        body={"subject": "agent-7"},
        control=cp,
    )
    assert status == 200 and payload == {"revoked": "agent-7"}
    assert rev.is_revoked("agent-7")
    app.close()


def test_admin_gate_decide_human_only_over_http():
    app, token, cp, _rev, gates = _rig()
    gates.require("deploy-prod", reason="irreversible")
    # a service token holding admin:gate is refused (opaque 404)
    svc = token("bot", [AdminScopes.GATE], service=True)
    status, _ = handle_request(
        app,
        "POST",
        "/admin/gates/decide",
        bearer=svc,
        body={"gate_id": "deploy-prod", "approved": True},
        control=cp,
    )
    assert status == 404
    # a human with the scope clears it
    human = token("alice", [AdminScopes.GATE])
    status2, payload2 = handle_request(
        app,
        "POST",
        "/admin/gates/decide",
        bearer=human,
        body={"gate_id": "deploy-prod", "approved": True},
        control=cp,
    )
    assert status2 == 200 and payload2 == {"gate": "deploy-prod", "status": "approved"}
    app.close()


def test_dashboard_and_admin_api_over_a_real_socket():
    """The dashboard is served as HTML at GET /admin (unauthenticated shell),
    and the admin API routes through the running server with the bearer."""
    import json
    import threading
    import urllib.request

    from hallpass.http_server import serve

    app, token, cp, _rev, _gates = _rig()
    server = serve(app, host="127.0.0.1", port=0, control=cp)
    host, port = server.server_address[0], server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # the dashboard shell: HTML, no auth
        with urllib.request.urlopen(f"http://{host}:{port}/admin", timeout=5) as r:
            assert r.status == 200
            assert "text/html" in r.headers.get("Content-Type", "")
            assert "hallpass control" in r.read().decode()
        # the gated API through the socket, with an admin bearer
        req = urllib.request.Request(
            f"http://{host}:{port}/admin/queue",
            headers={"Authorization": "Bearer " + token("ops", [AdminScopes.QUEUE])},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            assert "queue" in json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_admin_audit_tail_filters_over_http():
    app, token, cp, _rev, _gates = _rig()
    admin = token("ops", [AdminScopes.AUDIT])
    # generate a deny via a non-admin queue probe (audited by the control plane)
    handle_request(
        app, "GET", "/admin/queue", bearer=token("x", []), body=None, control=cp
    )
    status, payload = handle_request(
        app,
        "GET",
        "/admin/audit?decision=deny&limit=10",
        bearer=admin,
        body=None,
        control=cp,
    )
    assert status == 200
    assert all(e["decision"] == "deny" for e in payload["events"])
    app.close()
