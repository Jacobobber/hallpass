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

__all__ = ["ToolSpec", "ToolGate", "ToolDenied", "UnknownTool"]


class ToolDenied(Exception):
    """The principal lacks a required scope for this tool. Message names
    the tool and the missing scopes, never claim values."""


class UnknownTool(Exception):
    """No such tool is registered. Deliberately the same failure shape a
    denied caller sees from the catalog: unknown and ungranted are both
    'not there for you'."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_scopes: frozenset[str]
    handler: Callable[..., Any] = field(repr=False)
    connector: str = ""


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
        principal holds every required scope."""
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnknownTool(f"no tool named {tool_name!r}")
        missing = spec.required_scopes - principal.scopes
        if missing:
            raise ToolDenied(
                f"tool {tool_name!r} requires scopes {sorted(missing)}"
                " that were not granted"
            )
        return spec
