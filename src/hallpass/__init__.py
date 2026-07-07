"""hallpass: the multi-user auth core public MCP servers are missing.

Per-user OAuth 2.1 resource-server verification against any OIDC
provider, an encrypted per-user credential vault, and scope-derived tool
gating that is enforced at call time, not just in the catalog. Transport
comes last: the core is protocol-agnostic and the MCP wiring is a thin
adapter.
"""

from . import catalog
from .a2a import A2ABus, A2AMessage, ChannelDenied, ChannelPolicy
from .audit import AuditEvent, AuditSink, InMemoryAuditLog
from .connectors import Connector, UserContext
from .consent import Consent, ConsentLedger, InMemoryConsentLedger
from .core import Hallpass
from .diagnostics import Finding, doctor, format_report
from .gating import ToolDenied, ToolGate, ToolSpec, UnknownTool
from .identity import (
    HttpJwks,
    JwksSource,
    Principal,
    StaticJwks,
    TokenVerifier,
    VerificationError,
)
from .oauth import (
    HttpxTokenClient,
    InMemoryPendingStore,
    OAuthConnect,
    OAuthError,
    OAuthProvider,
    PendingStore,
    TokenHttp,
)
from .ratelimit import FixedWindowRateLimiter, RateLimited, RateLimiter
from .sanitize import frame_untrusted, sanitize
from .rest import (
    ConnectorError,
    Endpoint,
    HttpClient,
    HttpxClient,
    RestConnector,
    RestService,
    RetryingHttpClient,
    RetryPolicy,
    TokenRefresher,
)
from .search import LexicalRanker, ToolRanker, tokenize
from .server import build, dev_app
from .toolkit import ToolKit
from .vault import CredentialVault, VaultError

__version__ = "0.13.0"

__all__ = [
    "A2ABus",
    "A2AMessage",
    "AuditEvent",
    "AuditSink",
    "ChannelDenied",
    "ChannelPolicy",
    "Connector",
    "ConnectorError",
    "Consent",
    "ConsentLedger",
    "CredentialVault",
    "Endpoint",
    "Finding",
    "FixedWindowRateLimiter",
    "Hallpass",
    "HttpClient",
    "HttpJwks",
    "HttpxClient",
    "HttpxTokenClient",
    "InMemoryAuditLog",
    "InMemoryConsentLedger",
    "InMemoryPendingStore",
    "JwksSource",
    "LexicalRanker",
    "OAuthConnect",
    "OAuthError",
    "OAuthProvider",
    "PendingStore",
    "Principal",
    "RateLimited",
    "RateLimiter",
    "RestConnector",
    "RestService",
    "RetryPolicy",
    "RetryingHttpClient",
    "StaticJwks",
    "TokenHttp",
    "TokenRefresher",
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
    "catalog",
    "dev_app",
    "doctor",
    "format_report",
    "frame_untrusted",
    "sanitize",
    "tokenize",
]
