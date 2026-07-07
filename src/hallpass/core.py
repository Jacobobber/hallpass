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

from .audit import UNVERIFIED, AuditEvent, AuditSink
from .connectors import Connector, UserContext
from .gating import ToolGate, ToolSpec, UnknownTool
from .identity import Principal, TokenVerifier, VerificationError
from .ratelimit import RateLimited, RateLimiter
from .vault import CredentialVault

__all__ = ["Hallpass"]


class Hallpass:
    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        vault: CredentialVault,
        audit: AuditSink | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._verifier = verifier
        self._vault = vault
        self._audit_sink = audit
        self._rate_limiter = rate_limiter
        self._gate = ToolGate()
        self._services: dict[str, str] = {}  # tool name -> connector service
        self._unavailable: list[str] = []  # services skipped as unavailable

    def add_connector(self, connector: Connector) -> None:
        """Register a connector's tools. A connector may optionally
        implement ``available() -> bool``; if it reports False (its backend
        is not configured, say), its tools are not registered at all, so an
        unconfigured connector never advertises tools it cannot serve.
        Availability is read once, here at registration."""
        available = getattr(connector, "available", None)
        if callable(available) and available() is False:
            self._unavailable.append(connector.service)
            return
        for spec in connector.tools():
            named = ToolSpec(
                name=spec.name,
                description=spec.description,
                required_scopes=spec.required_scopes,
                handler=spec.handler,
                connector=connector.service,
                input_schema=spec.input_schema,
            )
            self._gate.register(named)
            self._services[spec.name] = connector.service

    @property
    def unavailable_connectors(self) -> list[str]:
        """Services skipped at registration because they reported
        unavailable; useful for a startup diagnostic."""
        return list(self._unavailable)

    def _record(
        self,
        action: str,
        subject: str,
        decision: str,
        *,
        tool: str | None = None,
        reason: str = "",
    ) -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                AuditEvent(
                    subject=subject,
                    action=action,
                    decision=decision,
                    tool=tool,
                    reason=reason,
                )
            )

    # -- the two transport-facing calls ------------------------------------

    def list_tools(self, token: str) -> list[ToolSpec]:
        """The catalog for whoever this token proves. Verification failure
        propagates; an unauthenticated caller has no catalog at all."""
        try:
            principal = self._verifier.verify(token)
        except VerificationError:
            self._record("list_tools", UNVERIFIED, "deny", reason="authentication")
            raise
        catalog = self._gate.catalog(principal)
        self._record("list_tools", principal.subject, "allow")
        return catalog

    def call_tool(self, token: str, name: str, arguments: dict[str, Any]) -> Any:
        """Verify, rate-limit, authorize at call time, then run the handler
        with a context scoped to this principal and this connector's
        service. Every outcome, allow or deny, is audited."""
        try:
            principal = self._verifier.verify(token)
        except VerificationError:
            self._record(
                "call_tool", UNVERIFIED, "deny", tool=name, reason="authentication"
            )
            raise

        if self._rate_limiter is not None:
            try:
                self._rate_limiter.check(principal.subject)
            except RateLimited:
                self._record(
                    "call_tool",
                    principal.subject,
                    "deny",
                    tool=name,
                    reason="rate_limited",
                )
                raise

        try:
            spec = self._gate.authorize(principal, name)
        except UnknownTool:
            # Covers both unknown and ungranted (ToolDenied subclasses it);
            # the reason stays opaque, matching the gate's own contract.
            self._record(
                "call_tool",
                principal.subject,
                "deny",
                tool=name,
                reason="not_authorized",
            )
            raise

        context = UserContext(
            principal=principal,
            _vault=self._vault,
            _service=self._services[name],
        )
        result = spec.handler(context, **arguments)
        self._record("call_tool", principal.subject, "allow", tool=name)
        return result

    # -- introspection used by adapters ------------------------------------

    def principal(self, token: str) -> Principal:
        return self._verifier.verify(token)
