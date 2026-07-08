"""GraphQL as a first-class endpoint type: a named operation carries a fixed
query document, and the tool's args become the GraphQL variables. What matters:
the request body is the {"query": <doc>, "variables": {...}} envelope (not the
caller hand-writing GraphQL), variables are only the provided args, and a
parameterless operation still sends an (empty) variables object."""

from hallpass import catalog, dev_app


class RecordingHttp:
    def __init__(self):
        self.last = None

    def request(self, method, url, *, headers, params, json, data=None):
        self.last = {"method": method, "url": url, "json": json}
        return {"data": {"ok": True}}


def _linear_app():
    http = RecordingHttp()
    app, token = dev_app(connectors=[catalog.load("linear", http=http)])
    app._vault.store("alice", "linear", "lin_api_key")
    return app, token, http


def test_named_operation_sends_query_envelope():
    app, token, http = _linear_app()
    out = app.call_tool(token("alice", ["linear:read"]), "linear_viewer", {})
    assert out == {"data": {"ok": True}}
    body = http.last["json"]
    assert body["query"].strip().startswith("query { viewer")
    assert body["variables"] == {}  # parameterless op still sends variables
    assert http.last["method"] == "POST" and http.last["url"].endswith("/graphql")
    app.close()


def test_operation_args_become_variables():
    app, token, http = _linear_app()
    app.call_tool(token("alice", ["linear:read"]), "linear_issue", {"id": "ISS-1"})
    body = http.last["json"]
    assert "$id" in body["query"]  # the fixed document declares the variable
    assert body["variables"] == {"id": "ISS-1"}  # arg mapped to the variable
    app.close()


def test_variables_include_only_provided_args():
    app, token, http = _linear_app()
    # linear_issue declares `id`; calling without it sends empty variables
    # (the server will reject the required var, which is its job, not ours)
    app.call_tool(token("alice", ["linear:read"]), "linear_issue", {})
    assert http.last["json"]["variables"] == {}
    app.close()


def test_graphql_tools_have_argument_schema_for_variables():
    tools = {t.name: t for t in catalog.load("linear").tools()}
    # the variable shows up as a tool argument
    schema = tools["linear_issue"].input_schema
    assert "id" in schema["properties"]
    # a parameterless op has no properties
    assert tools["linear_viewer"].input_schema["properties"] == {}
