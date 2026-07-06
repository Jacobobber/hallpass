"""Expose a Hallpass core as an MCP server.

This is the thin part on purpose. All the auth, isolation, and gating
live in the core; the adapter only translates between MCP's tool
protocol and the core's two calls, and answers one question the core
leaves open: where does the bearer token come from on each request.

That answer is injected. In a real deployment the token provider reads
the validated bearer from the transport's auth context (the ASGI scope
under streamable HTTP); in tests it returns a minted token. The adapter
does not care which, so the MCP wiring can be exercised with no server
and no network.

Requires the optional ``mcp`` extra: ``pip install hallpass[mcp]``.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool

from .core import Hallpass
from .gating import UnknownTool
from .identity import VerificationError

__all__ = ["build_mcp_server", "TokenProvider"]

# Returns the bearer token for the request in flight, or "" when none is
# present. Async so a provider may read it from an async transport context.
TokenProvider = Callable[[], Awaitable[str]]


def build_mcp_server(
    app: Hallpass,
    token_provider: TokenProvider,
    *,
    name: str = "hallpass",
) -> Server:
    """Wire a Hallpass core into an MCP low-level Server.

    The returned server is ready to hand to any MCP transport. Every
    list/call is gated by the core against the token the provider yields;
    an unauthenticated or under-scoped caller gets an MCP error, never a
    tool it was not granted.
    """
    server: Server = Server(name)

    # The MCP SDK's registration decorators are untyped upstream, so strict
    # mypy flags the handlers they wrap; the ignores are scoped to that.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        token = await token_provider()
        # An unauthenticated caller sees an empty catalog rather than an
        # error: listing is not an operation worth leaking token validity
        # through, and the call-time gate refuses use regardless.
        try:
            specs = app.list_tools(token)
        except VerificationError:
            return []
        return [
            Tool(
                name=spec.name,
                description=spec.description,
                inputSchema={"type": "object"},
            )
            for spec in specs
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        token = await token_provider()
        try:
            result = app.call_tool(token, name, arguments)
        except VerificationError:
            raise ValueError("authentication required") from None
        except UnknownTool as denied:
            # ToolDenied subclasses UnknownTool and carries the same opaque
            # message, so ungranted and nonexistent surface identically here:
            # a tool the caller cannot use is indistinguishable from one that
            # does not exist. The missing scopes never leave the process.
            raise ValueError(str(denied)) from None
        return [TextContent(type="text", text=_render(result))]

    return server


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)
