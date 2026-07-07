"""ToolKit turns decorated functions into a connector. The tests pin the
ergonomics that make it easy: name from the function, description from the
docstring, argument schema from the signature (context excluded), scopes
from the decorator, and it plugs into Hallpass like any other connector."""

import pytest

from hallpass import ToolKit, dev_app
from hallpass.toolkit import _schema_from_signature


def test_schema_excludes_context_and_maps_types():
    kit = ToolKit("svc")

    @kit.tool(scopes=["x:read"])
    def do_thing(ctx, name: str, count: int, ratio: float = 1.0):
        "Do a thing."
        return "ok"

    spec = kit.tools()[0]
    schema = spec.input_schema
    assert schema is not None
    # ctx is not an argument
    assert set(schema["properties"]) == {"name", "count", "ratio"}
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["ratio"]["type"] == "number"
    # only the params without a default are required
    assert set(schema["required"]) == {"name", "count"}


def test_name_and_description_defaults():
    kit = ToolKit("svc")

    @kit.tool()
    def read_note(ctx, id: str):
        "Read a note by id."
        return id

    spec = kit.tools()[0]
    assert spec.name == "read_note"
    assert spec.description == "Read a note by id."
    assert spec.connector == "svc"


def test_name_and_description_overrides():
    kit = ToolKit("svc")

    @kit.tool(name="fetch", description="custom")
    def internal_impl(ctx):
        return "x"

    spec = kit.tools()[0]
    assert spec.name == "fetch"
    assert spec.description == "custom"


def test_scopes_are_applied():
    kit = ToolKit("svc")

    @kit.tool(scopes=["a:read", "b:write"])
    def t(ctx):
        return "x"

    assert kit.tools()[0].required_scopes == {"a:read", "b:write"}


def test_duplicate_tool_name_rejected():
    kit = ToolKit("svc")

    @kit.tool()
    def dup(ctx):
        return 1

    with pytest.raises(ValueError):

        @kit.tool(name="dup")
        def other(ctx):
            return 2


def test_decorated_function_is_returned_unchanged():
    kit = ToolKit("svc")

    @kit.tool()
    def add(ctx, a: int, b: int):
        return a + b

    # still directly callable/testable, ctx ignored here
    assert add(None, 2, 3) == 5


def test_availability_gate():
    down = ToolKit("crm", available=lambda: False)
    assert down.available() is False
    up = ToolKit("notes")
    assert up.available() is True


def test_no_arg_tool_gets_empty_object_schema():
    schema = _schema_from_signature(lambda ctx: None)
    assert schema == {"type": "object", "properties": {}}


def test_toolkit_plugs_into_hallpass_end_to_end():
    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:read"])
    def read_note(ctx, id: str):
        "Read a note."
        return f"note {id} for {ctx.principal.subject}"

    app, token = dev_app(connectors=[kit])
    result = app.call_tool(token("alice", ["notes:read"]), "read_note", {"id": "7"})
    assert result == "note 7 for alice"
    # gating still holds: no scope, tool is not found
    assert app.list_tools(token("bob", [])) == []
    app.close()
