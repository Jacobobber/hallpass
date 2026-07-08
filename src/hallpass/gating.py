"""Scope-derived tool gating: a tool the user was not granted does not exist.

Two properties matter and both are tested. First, the catalog is
per-principal: listing returns only tools whose required scopes are a
subset of what the identity provider granted. Second, and the one naive
servers miss: gating is enforced at CALL time, not just list time --
hiding a tool from the menu is cosmetic; refusing to run it is security.
Deny is the default everywhere: no scopes means public tools only, and a
tool with no registered requirement is still a tool (empty requirement),
never a wildcard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .identity import Principal

__all__ = ["ToolSpec", "ToolAnnotations", "ToolGate", "ToolDenied", "UnknownTool"]


@dataclass(frozen=True)
class ToolAnnotations:
    """Behavioural hints about a tool, mirroring MCP's tool annotations. They
    are advisory metadata a client uses to decide how to present or guard a
    call (e.g. warn before a destructive one); hallpass carries them through
    from the catalog and advertises them, but access is still decided by
    scopes, not by these hints.

    - ``read_only``: the tool does not modify its environment (a GET).
    - ``destructive``: the tool may make irreversible changes (a DELETE).
    - ``idempotent``: repeating the call with the same args has no additional
      effect (a PUT).
    """

    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False


class UnknownTool(Exception):
    """No such tool is registered. Its message is the canonical opaque
    'not there for you' response, shared with ToolDenied so a caller
    cannot tell the two apart."""


class ToolDenied(UnknownTool):
    """The principal lacks a required scope for this tool.

    A subclass of UnknownTool with a byte-identical message on purpose:
    over the wire, an ungranted tool must be indistinguishable from one
    that does not exist, or a caller can enumerate the private tool
    namespace and read off the scope guarding each hidden tool. Trusted
    in-process code that needs the detail reads ``missing_scopes``; it is
    never placed in the message and never surfaced to a caller.
    """

    def __init__(self, message: str, *, missing_scopes: frozenset[str]) -> None:
        super().__init__(message)
        self.missing_scopes = missing_scopes


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_scopes: frozenset[str]
    handler: Callable[..., Any] = field(repr=False)
    connector: str = ""
    # JSON Schema for the tool's arguments. When set, the MCP adapter
    # advertises it so clients validate calls; when None it advertises an
    # open object (any arguments accepted).
    input_schema: dict[str, Any] | None = None
    # Advisory behaviour hints (read-only / destructive / idempotent),
    # advertised to clients. Never a substitute for scope gating.
    annotations: ToolAnnotations = field(default_factory=ToolAnnotations)


class ToolGate:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def catalog(self, principal: Principal) -> list[ToolSpec]:
        """Every tool this principal may call. Subset semantics: a tool
        requiring no scopes is visible to any authenticated principal."""
        return [
            spec
            for spec in self._tools.values()
            if spec.required_scopes <= principal.scopes
        ]

    def authorize(self, principal: Principal, tool_name: str) -> ToolSpec:
        """The call-time gate. Raises unless the tool exists AND the
        principal holds every required scope.

        Unknown and ungranted raise the same opaque message; ToolDenied
        (a subclass of UnknownTool) additionally carries ``missing_scopes``
        for trusted in-process callers, so type-based handling still works
        while the observable failure stays indistinguishable.
        """
        opaque = f"no tool named {tool_name!r}"
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnknownTool(opaque)
        missing = spec.required_scopes - principal.scopes
        if missing:
            raise ToolDenied(opaque, missing_scopes=frozenset(missing))
        return spec
