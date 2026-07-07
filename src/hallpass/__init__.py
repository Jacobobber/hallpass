"""hallpass: the multi-user auth core public MCP servers are missing.

Per-user OAuth 2.1 resource-server verification against any OIDC
provider, an encrypted per-user credential vault, and scope-derived tool
gating that is enforced at call time, not just in the catalog. Transport
comes last: the core is protocol-agnostic and the MCP wiring is a thin
adapter.
"""

from .a2a import A2ABus, A2AMessage, ChannelDenied, ChannelPolicy
from .audit import AuditEvent, AuditSink, InMemoryAuditLog
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
from .ratelimit import FixedWindowRateLimiter, RateLimited, RateLimiter
from .search import LexicalRanker, ToolRanker, tokenize
from .server import build, dev_app
from .toolkit import ToolKit
from .vault import CredentialVault, VaultError

__version__ = "0.4.0"

__all__ = [
    "A2ABus",
    "A2AMessage",
    "AuditEvent",
    "AuditSink",
    "ChannelDenied",
    "ChannelPolicy",
    "Connector",
    "CredentialVault",
    "FixedWindowRateLimiter",
    "Hallpass",
    "HttpJwks",
    "InMemoryAuditLog",
    "JwksSource",
    "LexicalRanker",
    "Principal",
    "RateLimited",
    "RateLimiter",
    "StaticJwks",
    "TokenVerifier",
    "ToolDenied",
    "ToolGate",
    "ToolKit",
    "ToolRanker",
    "ToolSpec",
    "UnknownTool",
    "UserContext",
    "VaultError",
    "VerificationError",
    "build",
    "dev_app",
    "tokenize",
]
