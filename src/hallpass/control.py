"""The control plane: a gated admin + observability surface over a running fleet.

This is the auth layer at operator scale. Every call is the SAME
``TokenVerifier`` -> ``Principal`` that gates a tool call, then an admin-scope
check, then the action, then an audit record -- so watching a queue, reading the
audit tail, revoking an agent, and approving a held human gate are all governed
by the one decision the rest of hallpass is built on. There is deliberately **no
second or weaker auth path**: an admin capability is a scope, never a backdoor
token, a shared secret, or a trusted proxy header. Deny is the default and it is
opaque -- an unauthenticated caller, one lacking the scope, and one asking for an
unwired subsystem all get the same ``ControlDenied``, so a non-admin cannot map
the surface.

The observability reads (``queue_depth``, ``audit_tail``) go through subsystems
whose data is operator-level by nature; the audit tail reads the *shared* audit
store, so in a multi-replica deployment it is the whole fleet's trail, not one
replica's slice. Destructive actions reuse the primitives that already enforce
their own rule: ``approve_gate`` delegates to the ``HumanGateLedger``, which
refuses a service principal even one holding the admin scope, so a machine token
can never clear a human gate.
"""

from __future__ import annotations

from .audit import AuditEvent, AuditSink
from .humangate import HumanGateError, HumanGateLedger
from .identity import Principal, RevocationList, TokenVerifier, VerificationError
from .taskqueue import TaskQueue

__all__ = ["ControlPlane", "ControlDenied", "AdminScopes"]

UNVERIFIED = "<unverified>"


class ControlDenied(Exception):
    """The caller may not perform this control-plane action -- unauthenticated,
    missing the admin scope, or the subsystem is not wired. Deliberately opaque:
    the same error in every case, so a non-admin cannot tell a capability that
    exists but isn't theirs from one that isn't configured."""


class AdminScopes:
    """The scopes that grant control-plane capabilities. Grant them like any
    other scope (a role, a delegation); holding one IS the capability. They are
    ordinary scopes, so nothing about the admin surface lives outside the model
    that gates everything else."""

    QUEUE = "admin:queue"  # read queue depth
    AUDIT = "admin:audit"  # read the audit tail
    REVOKE = "admin:revoke"  # revoke / restore an agent identity
    GATE = "admin:gate"  # approve / deny a held human gate


class ControlPlane:
    """Gated admin + observability over the running fleet. Wire only the
    subsystems you want to expose; a call into an unwired one denies opaquely,
    exactly like a missing scope."""

    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        audit: AuditSink | None = None,
        queue: TaskQueue | None = None,
        revocations: RevocationList | None = None,
        gates: HumanGateLedger | None = None,
    ) -> None:
        self._verifier = verifier
        self._audit = audit
        self._queue = queue
        self._revocations = revocations
        self._gates = gates

    def _record(
        self,
        subject: str,
        action: str,
        decision: str,
        *,
        reason: str = "",
        target: str | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                AuditEvent(
                    subject=subject,
                    action=action,
                    decision=decision,
                    tool=target,
                    reason=reason,
                )
            )

    def _authorize(self, token: str, scope: str, action: str) -> Principal:
        try:
            principal = self._verifier.verify(token)
        except VerificationError:
            self._record(UNVERIFIED, action, "deny", reason="authentication")
            raise ControlDenied("not permitted") from None
        if scope not in principal.scopes:
            self._record(principal.subject, action, "deny", reason="not_authorized")
            raise ControlDenied("not permitted")
        return principal

    # -- observability -----------------------------------------------------

    def queue_depth(self, token: str) -> dict[str, object]:
        """Pending count and the outstanding (in-flight + queued) task ids, for
        an operator watching backlog. Requires ``admin:queue``."""
        principal = self._authorize(token, AdminScopes.QUEUE, "control_queue_depth")
        if self._queue is None:
            raise ControlDenied("not permitted")
        depth = {
            "pending": self._queue.pending_count(),
            "outstanding": list(self._queue.outstanding()),
        }
        self._record(principal.subject, "control_queue_depth", "allow")
        return depth

    def audit_tail(
        self,
        token: str,
        *,
        subject: str | None = None,
        decision: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """The most recent audit events (newest first), optionally filtered.
        Reads the shared audit store, so under multiple replicas this is the
        whole fleet's trail. Requires ``admin:audit`` and a queryable audit
        sink (``SqliteAuditLog`` / ``PostgresAuditLog``)."""
        principal = self._authorize(token, AdminScopes.AUDIT, "control_audit_tail")
        query = getattr(self._audit, "query", None)
        if not callable(query):
            raise ControlDenied("not permitted")
        events: list[AuditEvent] = query(
            subject=subject,
            decision=decision,
            action=action,
            since=since,
            limit=limit,
        )
        self._record(
            principal.subject, "control_audit_tail", "allow", reason=f"n={len(events)}"
        )
        return events

    # -- admin actions -----------------------------------------------------

    def revoke_agent(self, token: str, subject: str, *, reason: str = "") -> None:
        """Revoke an agent identity: every live token for ``subject`` stops
        verifying on its next request. Requires ``admin:revoke``. This is a
        capability (a trusted incident-response service may hold it); wire it
        behind a human gate if your policy requires a person."""
        principal = self._authorize(token, AdminScopes.REVOKE, "control_revoke")
        revoke = getattr(self._revocations, "revoke", None)
        if not callable(revoke):
            raise ControlDenied("not permitted")
        revoke(subject, reason=reason)
        self._record(
            principal.subject, "control_revoke", "allow", target=subject, reason=reason
        )

    def restore_agent(self, token: str, subject: str) -> None:
        """Lift a revocation (e.g. after re-provisioning). Requires
        ``admin:revoke``."""
        principal = self._authorize(token, AdminScopes.REVOKE, "control_restore")
        restore = getattr(self._revocations, "restore", None)
        if not callable(restore):
            raise ControlDenied("not permitted")
        restore(subject)
        self._record(principal.subject, "control_restore", "allow", target=subject)

    def revoked_agents(self, token: str) -> list[str]:
        """The currently revoked subjects. Requires ``admin:revoke``."""
        principal = self._authorize(token, AdminScopes.REVOKE, "control_revoked")
        revoked = getattr(self._revocations, "revoked", None)
        if not callable(revoked):
            raise ControlDenied("not permitted")
        out: list[str] = list(revoked())
        self._record(principal.subject, "control_revoked", "allow")
        return out

    # -- human gates -------------------------------------------------------

    def pending_gates(self, token: str) -> list[str]:
        """Gate ids awaiting a human decision. Requires ``admin:gate``."""
        principal = self._authorize(token, AdminScopes.GATE, "control_pending_gates")
        if self._gates is None:
            raise ControlDenied("not permitted")
        ids = self._gates.pending()
        self._record(principal.subject, "control_pending_gates", "allow")
        return ids

    def decide_gate(
        self, token: str, gate_id: str, *, approved: bool = True, note: str = ""
    ) -> str:
        """Record a decision on a held human gate and return its new status.
        Requires ``admin:gate`` AND that the caller is a **human** -- the gate
        ledger refuses a service principal even one holding the scope, so a
        machine token can never clear a human gate. An unknown gate and a
        service caller both deny opaquely (the audit carries the reason)."""
        principal = self._authorize(token, AdminScopes.GATE, "control_decide_gate")
        if self._gates is None:
            raise ControlDenied("not permitted")
        try:
            gate = self._gates.decide(gate_id, principal, approved=approved, note=note)
        except HumanGateError:
            self._record(
                principal.subject,
                "control_decide_gate",
                "deny",
                target=gate_id,
                reason="human_gate_refused",
            )
            raise ControlDenied("not permitted") from None
        except KeyError:
            self._record(
                principal.subject,
                "control_decide_gate",
                "deny",
                target=gate_id,
                reason="no_such_gate",
            )
            raise ControlDenied("not permitted") from None
        self._record(
            principal.subject,
            "control_decide_gate",
            "allow",
            target=gate_id,
            reason=gate.status,
        )
        return gate.status
