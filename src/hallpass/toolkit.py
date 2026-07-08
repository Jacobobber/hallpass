"""Define a connector by decorating functions, no boilerplate.

A ``ToolKit`` turns plain functions into tools: the tool's name comes from
the function, its description from the docstring, and its argument schema
from the signature. Required scopes are declared on the decorator. Hand the
kit straight to ``Hallpass.add_connector``.

    kit = ToolKit("notes")

    @kit.tool(scopes=["notes:read"])
    def read_note(ctx, id: str):
        "Read a note by id."
        return ctx.credential() and f"note {id}"

Every tool receives the ``UserContext`` as its first parameter (call it
``ctx``); the remaining parameters become the tool's arguments and are
excluded-of-context in the generated schema.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .gating import ToolAnnotations, ToolSpec

__all__ = ["ToolKit"]

F = TypeVar("F", bound=Callable[..., Any])

_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """A JSON Schema for the tool's arguments, derived from the function's
    parameters. The leading context parameter is dropped; annotated types
    map to JSON types; parameters without a default are required."""
    params = list(inspect.signature(fn).parameters.values())
    if params:
        params = params[1:]  # drop the context parameter
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        prop: dict[str, Any] = {}
        if isinstance(p.annotation, type) and p.annotation in _JSON_TYPES:
            prop["type"] = _JSON_TYPES[p.annotation]
        properties[p.name] = prop
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


class ToolKit:
    """A connector assembled from decorated functions. Optionally pass
    ``available`` (a callable) to gate the whole kit on backend
    configuration; if it returns False the kit's tools are not registered."""

    def __init__(
        self, service: str, *, available: Callable[[], bool] | None = None
    ) -> None:
        self.service = service
        self._available = available
        self._tools: list[ToolSpec] = []

    def tool(
        self,
        *,
        scopes: Iterable[str] = (),
        name: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
    ) -> Callable[[F], F]:
        """Register the decorated function as a tool. Returns the function
        unchanged, so it stays directly callable and testable. Pass
        ``annotations`` to hint read-only/destructive/idempotent to clients."""

        def decorator(fn: F) -> F:
            tool_name = name or fn.__name__
            if any(t.name == tool_name for t in self._tools):
                raise ValueError(f"tool {tool_name!r} is already defined in this kit")
            doc = (inspect.getdoc(fn) or "").strip()
            self._tools.append(
                ToolSpec(
                    name=tool_name,
                    description=description or doc or tool_name,
                    required_scopes=frozenset(scopes),
                    handler=fn,
                    connector=self.service,
                    input_schema=_schema_from_signature(fn),
                    annotations=annotations or ToolAnnotations(),
                )
            )
            return fn

        return decorator

    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def available(self) -> bool:
        return self._available() if self._available is not None else True
