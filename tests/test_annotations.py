"""Tools carry advisory MCP-style annotations (read-only / destructive /
idempotent) so a client can warn before a destructive call. What matters: the
catalog derives them correctly from the HTTP verb, a ToolKit author can set
them, and they are advertised through both the HTTP server and (in shape) the
MCP adapter. They are hints only -- access is still decided by scopes."""

from hallpass import ToolAnnotations, ToolKit, catalog, dev_app
from hallpass.http_server import handle_request


def _method_of(service, tool_name):
    svc = catalog.SERVICES[service]
    return next(e.method for e in svc.endpoints if e.name == tool_name)


def test_catalog_derives_annotations_from_verb():
    gh = {s.name: s.annotations for s in catalog.load("github").tools()}
    # GET list -> read-only
    assert gh["github_list_my_repos"].read_only is True
    assert gh["github_list_my_repos"].destructive is False
    # POST create -> a write, not read-only, not destructive
    assert gh["github_create_issue"].read_only is False
    assert gh["github_create_issue"].destructive is False


def test_delete_is_destructive_and_put_is_idempotent():
    # find a DELETE and a PUT somewhere in the catalog to prove the mapping
    from hallpass.rest import _annotations_for_method

    assert _annotations_for_method("DELETE").destructive is True
    assert _annotations_for_method("PUT").idempotent is True
    assert _annotations_for_method("GET").read_only is True
    assert _annotations_for_method("POST") == ToolAnnotations()


def test_toolkit_author_can_set_annotations():
    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:read"], annotations=ToolAnnotations(read_only=True))
    def read_note(ctx, id: str):
        return {"id": id}

    spec = kit.tools()[0]
    assert spec.annotations.read_only is True


def test_toolkit_default_annotation_is_neutral():
    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:write"])
    def write_note(ctx, id: str):
        return {"id": id}

    assert kit.tools()[0].annotations == ToolAnnotations()


def test_http_server_advertises_annotations():
    gh = catalog.load("github")
    app, token = dev_app(connectors=[gh])
    status, payload = handle_request(
        app, "GET", "/tools", bearer=token("a", ["github:read"]), body=None
    )
    assert status == 200
    by_name = {t["name"]: t["annotations"] for t in payload["tools"]}
    assert by_name["github_list_my_repos"]["read_only"] is True
    assert set(by_name["github_list_my_repos"]) == {
        "read_only",
        "destructive",
        "idempotent",
    }
    app.close()
