"""The HTTP reference server must gate exactly like the core: the catalog is
per-bearer, an ungranted tool is indistinguishable from a nonexistent one (same
404), an invalid bearer is 401, and no response body carries the bearer or a
credential. handle_request is pure, so this runs without opening a socket."""

from hallpass import ToolKit, dev_app
from hallpass.http_server import bearer_from_header, handle_request


def _app():
    kit = ToolKit("demo")

    @kit.tool(scopes=["demo:read"], name="ping", description="return pong")
    def ping(ctx, **kwargs):
        return {"pong": True}

    return dev_app(connectors=[kit])


def test_bearer_from_header():
    assert bearer_from_header("Bearer abc.def") == "abc.def"
    assert bearer_from_header("bearer abc") == "abc"  # scheme is case-insensitive
    assert bearer_from_header("Basic abc") == ""
    assert bearer_from_header(None) == ""
    assert bearer_from_header("") == ""


def test_healthz_needs_no_auth():
    app, _ = _app()
    assert handle_request(app, "GET", "/healthz", bearer="", body=None) == (
        200,
        {"status": "ok"},
    )
    app.close()


def test_tools_list_is_per_bearer():
    app, token = _app()
    status, payload = handle_request(
        app, "GET", "/tools", bearer=token("alice", ["demo:read"]), body=None
    )
    assert status == 200
    assert [t["name"] for t in payload["tools"]] == ["ping"]
    # a caller without the scope sees an empty catalog, not the tool
    status2, payload2 = handle_request(
        app, "GET", "/tools", bearer=token("bob", []), body=None
    )
    assert status2 == 200 and payload2["tools"] == []
    app.close()


def test_invalid_bearer_is_401():
    app, _ = _app()
    status, payload = handle_request(app, "GET", "/tools", bearer="garbage", body=None)
    assert status == 401
    assert "garbage" not in str(payload)  # never echo the bearer
    app.close()


def test_call_success():
    app, token = _app()
    status, payload = handle_request(
        app,
        "POST",
        "/call/ping",
        bearer=token("alice", ["demo:read"]),
        body={"arguments": {}},
    )
    assert status == 200 and payload == {"result": {"pong": True}}
    app.close()


def test_unknown_and_ungranted_tool_are_the_same_404():
    app, token = _app()
    # nonexistent tool
    s1, _ = handle_request(
        app, "POST", "/call/nope", bearer=token("alice", ["demo:read"]), body={}
    )
    # real tool, missing scope
    s2, p2 = handle_request(
        app, "POST", "/call/ping", bearer=token("alice", []), body={}
    )
    assert s1 == s2 == 404
    assert "scope" not in str(p2).lower()  # don't leak why it was denied
    app.close()


def test_call_without_auth_is_401():
    app, _ = _app()
    status, _ = handle_request(app, "POST", "/call/ping", bearer="", body={})
    assert status == 401
    app.close()


def test_unknown_route_is_404():
    app, _ = _app()
    assert handle_request(app, "GET", "/nope", bearer="", body=None)[0] == 404
    app.close()


def test_connector_error_is_opaque_502_without_backend_detail():
    from hallpass import catalog

    class FailingHttp:
        def request(self, method, url, *, headers, params, json, data=None):
            from hallpass import ConnectorError

            raise ConnectorError(f"{method} {url} failed: HTTP 500", status=500)

    app, token = dev_app(connectors=[catalog.load("github", http=FailingHttp())])
    app._vault.store("alice", "github", "ghp_secret")
    status, payload = handle_request(
        app,
        "POST",
        "/call/github_list_my_repos",
        bearer=token("alice", ["github:read"]),
        body={},
    )
    assert status == 502
    assert payload == {"error": "upstream service error", "status": 500}
    assert "api.github.com" not in str(payload)  # backend host not disclosed
    app.close()


def test_non_serializable_result_is_opaque_500():
    kit = ToolKit("weird")

    @kit.tool(scopes=["weird:x"], name="setty", description="returns a set")
    def setty(ctx, **kwargs):
        return {1, 2, 3}  # not JSON-serializable

    app, token = dev_app(connectors=[kit])
    status, payload = handle_request(
        app, "POST", "/call/setty", bearer=token("a", ["weird:x"]), body={}
    )
    assert status == 500 and payload == {"error": "internal error"}
    app.close()


def test_unexpected_handler_error_is_opaque_500():
    kit = ToolKit("boom")

    @kit.tool(scopes=["boom:x"], name="explode", description="raises")
    def explode(ctx, **kwargs):
        raise RuntimeError("secret internal detail")

    app, token = dev_app(connectors=[kit])
    status, payload = handle_request(
        app, "POST", "/call/explode", bearer=token("a", ["boom:x"]), body={}
    )
    assert status == 500
    assert payload == {"error": "internal error"}  # no traceback, no detail
    assert "secret" not in str(payload)
    app.close()
