"""hallpass: the multi-user auth core public MCP servers are missing.

Per-user OAuth 2.1 resource-server verification against any OIDC
provider, an encrypted per-user credential vault, and scope-derived tool
gating that is enforced at call time, not just in the catalog. Transport
comes last: the core is protocol-agnostic and the MCP wiring is a thin
adapter.
"""

from . import catalog, flex
from .a2a import A2ABus, A2AMessage, ChannelDenied, ChannelPolicy
from .agents import (
    AgentContext,
    AgentHandle,
    AgentSpec,
    Spawner,
    SubprocessSpawner,
    Team,
)
from .audit import AuditEvent, AuditSink, InMemoryAuditLog, SqliteAuditLog
from .connectors import Connector, UserContext
from .consent import (
    Consent,
    ConsentLedger,
    InMemoryConsentLedger,
    SqliteConsentLedger,
)
from .core import Hallpass
from .diagnostics import Finding, doctor, format_report
from .dm import DirectChannel, direct_channel, open_dm
from .gating import ToolAnnotations, ToolDenied, ToolGate, ToolSpec, UnknownTool
from .guard import TRUNCATED_KEY, guard_response
from .identity import (
    HttpJwks,
    JwksSource,
    Principal,
    StaticJwks,
    TokenVerifier,
    VerificationError,
)
from .idempotency import IdempotencyStore, InMemoryIdempotencyStore
from .oauth import (
    HttpxTokenClient,
    InMemoryPendingStore,
    OAuthConnect,
    OAuthError,
    OAuthProvider,
    PendingStore,
    SqlitePendingStore,
    TokenHttp,
)
from .orchestrator import Handler, Orchestrator, Result, Router, Task, Worker
from .taskqueue import LeasedTask, TaskQueue
from .ratelimit import FixedWindowRateLimiter, RateLimited, RateLimiter
from .sanitize import frame_untrusted, sanitize
from .rest import (
    BreakerPolicy,
    CircuitBreakerHttpClient,
    CircuitOpen,
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
from .runner import QueueHandler, run_worker, serve_queue
from .search import LexicalRanker, ToolRanker, tokenize
from .server import build, dev_app
from .toolkit import ToolKit
from .vault import CredentialVault, VaultError

__version__ = "1.10.0"

__all__ = [
    "A2ABus",
    "A2AMessage",
    "AgentContext",
    "AgentHandle",
    "AgentSpec",
    "AuditEvent",
    "AuditSink",
    "BreakerPolicy",
    "ChannelDenied",
    "ChannelPolicy",
    "CircuitBreakerHttpClient",
    "CircuitOpen",
    "Connector",
    "ConnectorError",
    "Consent",
    "ConsentLedger",
    "CredentialVault",
    "DirectChannel",
    "Endpoint",
    "Finding",
    "FixedWindowRateLimiter",
    "Hallpass",
    "Handler",
    "HttpClient",
    "HttpJwks",
    "HttpxClient",
    "HttpxTokenClient",
    "IdempotencyStore",
    "InMemoryAuditLog",
    "InMemoryConsentLedger",
    "InMemoryIdempotencyStore",
    "InMemoryPendingStore",
    "JwksSource",
    "LeasedTask",
    "LexicalRanker",
    "OAuthConnect",
    "OAuthError",
    "OAuthProvider",
    "Orchestrator",
    "PendingStore",
    "Principal",
    "QueueHandler",
    "Result",
    "RateLimited",
    "RateLimiter",
    "RestConnector",
    "RestService",
    "RetryPolicy",
    "RetryingHttpClient",
    "Router",
    "SqliteAuditLog",
    "SqliteConsentLedger",
    "SqlitePendingStore",
    "Spawner",
    "StaticJwks",
    "SubprocessSpawner",
    "Task",
    "TaskQueue",
    "Team",
    "TokenHttp",
    "TokenRefresher",
    "TokenVerifier",
    "ToolAnnotations",
    "ToolDenied",
    "ToolGate",
    "ToolKit",
    "ToolRanker",
    "ToolSpec",
    "TRUNCATED_KEY",
    "UnknownTool",
    "UserContext",
    "VaultError",
    "Worker",
    "VerificationError",
    "build",
    "catalog",
    "dev_app",
    "direct_channel",
    "doctor",
    "flex",
    "format_report",
    "frame_untrusted",
    "guard_response",
    "open_dm",
    "run_worker",
    "sanitize",
    "serve_queue",
    "tokenize",
]
