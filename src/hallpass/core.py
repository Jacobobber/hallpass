"""The transport-agnostic core: token in, gated tool call out.

Hallpass composes the three layers -- verify the token, gate the catalog,
hand the handler a per-user context -- behind two methods that any
transport can call: list_tools(token) and call_tool(token, name, args).
The MCP adapter (hallpass.mcp_adapter) is one thin consumer of this; a
plain HTTP API or a test can be another. Keeping the core free of
transport means the security suite runs with zero network and no
protocol mocks.
"""

from __future__ import annotations

from typing import Any

from .connectors import Connector, UserContext
from .gating import ToolGate, ToolSpec
from .identity import Principal, TokenVerifier
from .vault import CredentialVault

__all__ = ["Hallpass"]


class Hallpass:
    def __init__(self, *, verifier: TokenVerifier, vault: CredentialVault) -> None:
        self._verifier = verifier
        self._vault = vault
        self._gate = ToolGate()
        self._services: dict[str, str] = {}  # tool name -> connector service

    def add_connector(self, connector: Connector) -> None:
        for spec in connector.tools():
            named = ToolSpec(
                name=spec.name,
                description=spec.description,
                required_scopes=spec.required_scopes,
                handler=spec.handler,
                connector=connector.service,
            )
            self._gate.register(named)
            self._services[spec.name] = connector.service

    # -- the two transport-facing calls ------------------------------------

    def list_tools(self, token: str) -> list[ToolSpec]:
        """The catalog for whoever this token proves. Verification failure
        propagates; an unauthenticated caller has no catalog at all."""
        principal = self._verifier.verify(token)
        return self._gate.catalog(principal)

    def call_tool(self, token: str, name: str, arguments: dict[str, Any]) -> Any:
        """Verify, authorize at call time, then run the handler with a
        context scoped to this principal and this connector's service."""
        principal = self._verifier.verify(token)
        spec = self._gate.authorize(principal, name)
        context = UserContext(
            principal=principal,
            _vault=self._vault,
            _service=self._services[name],
        )
        return spec.handler(context, **arguments)

    # -- introspection used by adapters ------------------------------------

    def principal(self, token: str) -> Principal:
        return self._verifier.verify(token)
