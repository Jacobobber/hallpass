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

import time
from typing import Any

from .audit import UNVERIFIED, AuditEvent, AuditSink
from .connectors import Connector, UserContext
from .gating import ToolGate, ToolSpec, UnknownTool
from .identity import Principal, TokenVerifier, VerificationError
from .idempotency import IdempotencyStore
from .ratelimit import RateLimited, RateLimiter
from .search import LexicalRanker, ToolRanker
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
        ranker: ToolRanker | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._verifier = verifier
        self._vault = vault
        self._audit_sink = audit
        self._rate_limiter = rate_limiter
        self._ranker = ranker or LexicalRanker()
        self._idempotency = idempotency
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
                annotations=spec.annotations,
            )
            self._gate.register(named)
            self._services[spec.name] = connector.service

    @property
    def unavailable_connectors(self) -> list[str]:
        """Services skipped at registration because they reported
        unavailable; useful for a startup diagnostic."""
        return list(self._unavailable)

    # -- read-only introspection (used by hallpass.diagnostics.doctor) -----

    @property
    def tool_names(self) -> list[str]:
        """Every registered tool name, sorted."""
        return sorted(self._services)

    @property
    def connector_names(self) -> list[str]:
        """Every connector service that registered at least one tool."""
        return sorted(set(self._services.values()))

    @property
    def has_audit(self) -> bool:
        return self._audit_sink is not None

    @property
    def has_rate_limiter(self) -> bool:
        return self._rate_limiter is not None

    @property
    def vault_durable(self) -> bool:
        """Whether connected credentials survive a restart (see the vault)."""
        return self._vault.durable

    def _record(
        self,
        action: str,
        subject: str,
        decision: str,
        *,
        tool: str | None = None,
        reason: str = "",
        duration_ms: float | None = None,
    ) -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                AuditEvent(
                    subject=subject,
                    action=action,
                    decision=decision,
                    tool=tool,
                    reason=reason,
                    duration_ms=duration_ms,
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

    def search_tools(
        self, token: str, query: str, *, limit: int = 10
    ) -> list[ToolSpec]:
        """Rank the caller's AUTHORIZED tools by relevance to ``query`` and
        return the top ``limit``. Gating runs first, so the ranker only sees
        the authorized set, AND the core re-filters the ranker's output back
        against that set by name: search can never surface a tool the caller
        could not call, even if a custom or misbehaving ranker tries to add
        one. The invariant is the core's, not a trusted ranker contract. The
        query text is not recorded; only the hit count is audited (a query
        may carry sensitive content)."""
        try:
            principal = self._verifier.verify(token)
        except VerificationError:
            self._record("search_tools", UNVERIFIED, "deny", reason="authentication")
            raise
        authorized = self._gate.catalog(principal)
        allowed = {spec.name for spec in authorized}
        ranked = [
            spec
            for spec in self._ranker.rank(query, authorized)
            if spec.name in allowed
        ][: max(limit, 0)]
        self._record(
            "search_tools", principal.subject, "allow", reason=f"hits={len(ranked)}"
        )
        return ranked

    def call_tool(
        self,
        token: str,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        """Verify, rate-limit, authorize at call time, then run the handler
        with a context scoped to this principal and this connector's service.
        Every outcome, allow or deny, is audited.

        When ``idempotency_key`` is given and an idempotency store is wired, a
        repeat of the same ``(subject, tool, key)`` returns the first call's
        stored result instead of running the handler again -- so a retried
        mutation happens at most once. Only successful results are remembered.
        """
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

        # Idempotency check runs after authorization, so an unauthorized caller
        # can never probe the result cache. A hit short-circuits the handler.
        store = self._idempotency if idempotency_key is not None else None
        if store is not None and idempotency_key is not None:
            hit, cached = store.get(principal.subject, name, idempotency_key)
            if hit:
                self._record(
                    "call_tool", principal.subject, "allow", tool=name, reason="replay"
                )
                return cached

        context = UserContext(
            principal=principal,
            _vault=self._vault,
            _service=self._services[name],
        )
        started = time.monotonic()
        result = spec.handler(context, **arguments)
        duration_ms = (time.monotonic() - started) * 1000.0
        # Remember only on success: a failed call leaves nothing, so a genuine
        # retry can still go through.
        if store is not None and idempotency_key is not None:
            store.put(principal.subject, name, idempotency_key, result)
        self._record(
            "call_tool", principal.subject, "allow", tool=name, duration_ms=duration_ms
        )
        return result

    # -- introspection used by adapters ------------------------------------

    def principal(self, token: str) -> Principal:
        return self._verifier.verify(token)

    def close(self) -> None:
        """Release the credential vault's resources. Call when done with an
        app you built (the factory owns the vault it created)."""
        self._vault.close()
