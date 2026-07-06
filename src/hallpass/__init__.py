"""hallpass: the multi-user auth core public MCP servers are missing.

Per-user OAuth 2.1 resource-server verification against any OIDC
provider, an encrypted per-user credential vault, and scope-derived tool
gating that is enforced at call time, not just in the catalog. Transport
comes last: the core is protocol-agnostic and the MCP wiring is a thin
adapter.
"""

from .connectors import Connector, UserContext
from .core import Hallpass
from .gating import ToolDenied, ToolGate, ToolSpec, UnknownTool
from .identity import (
    HttpJwks,
    JwksSource,
    Principal,
    StaticJwks,
    TokenVerifier,
    VerificationError,
)
from .vault import CredentialVault, VaultError

__version__ = "0.1.0"

__all__ = [
    "Connector",
    "CredentialVault",
    "Hallpass",
    "HttpJwks",
    "JwksSource",
    "Principal",
    "StaticJwks",
    "TokenVerifier",
    "ToolDenied",
    "ToolGate",
    "ToolSpec",
    "UnknownTool",
    "UserContext",
    "VaultError",
    "VerificationError",
]
