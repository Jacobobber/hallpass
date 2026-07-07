"""The connector seam: how a service plugs its tools into the server.

A connector declares tools (name, description, required scopes, handler)
and receives a UserContext when a tool runs: the authenticated principal
plus vault access scoped to that principal. The connector never sees the
vault itself, so it cannot read another user's credentials even by bug --
the isolation lives in the seam, not in connector discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .gating import ToolSpec
from .identity import Principal
from .vault import CredentialVault

__all__ = ["UserContext", "Connector"]


@dataclass(frozen=True)
class UserContext:
    """What a tool handler is allowed to know: who is calling, and that
    caller's credentials for THIS connector's service only."""

    principal: Principal
    _vault: CredentialVault
    _service: str

    def credential(self) -> str | None:
        """The calling user's stored credential for this connector's
        service, or None when they have not connected it."""
        return self._vault.fetch(self.principal.subject, self._service)

    def store_credential(self, secret: str) -> None:
        self._vault.store(self.principal.subject, self._service, secret)


class Connector(Protocol):
    """Implement this to add a service. `service` names the credential
    slot in the vault; `tools()` declares what the connector offers.

    Optionally implement ``available(self) -> bool``. When a connector
    reports False at registration (its backend is not configured, a
    required key is missing), Hallpass skips registering its tools, so an
    unconfigured connector never advertises tools it cannot serve. A
    connector that omits ``available`` is always treated as available.
    """

    service: str

    def tools(self) -> list[ToolSpec]: ...
