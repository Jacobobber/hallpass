"""The whole thing in one runnable file, on the core install alone.

    python examples/quickstart.py

Defines a connector by decorating functions, spins up a zero-config dev
server (no identity provider), and shows per-user scope gating end to end.
"""

from hallpass import ToolKit, dev_app

kit = ToolKit("notes")


@kit.tool(scopes=["notes:read"])
def read_note(ctx, id: str):
    "Read the caller's note by id."
    return f"note {id} for {ctx.principal.subject}"


@kit.tool(scopes=["notes:write"])
def write_note(ctx, id: str, body: str):
    "Write a note."
    return f"wrote note {id} for {ctx.principal.subject}"


def main() -> None:
    app, token = dev_app(connectors=[kit])

    reader = token("alice", ["notes:read"])
    print("alice can see:", [t.name for t in app.list_tools(reader)])
    print("alice reads:  ", app.call_tool(reader, "read_note", {"id": "7"}))
    print("alice search: ", [t.name for t in app.search_tools(reader, "read a note")])

    writer = token("bob", ["notes:write"])
    print("bob can see:  ", [t.name for t in app.list_tools(writer)])
    try:
        app.call_tool(writer, "read_note", {"id": "7"})  # bob lacks notes:read
    except Exception as exc:  # noqa: BLE001 - demo
        print("bob read:     refused ->", exc)

    app.close()


if __name__ == "__main__":
    main()
