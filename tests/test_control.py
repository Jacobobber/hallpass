"""The control plane is the auth layer at operator scale: every call is
verify -> admin-scope check -> action -> audit, with no second/weaker auth path
and opaque deny by default. These pin that each capability needs its scope, that
an unauthenticated caller and one lacking the scope fail identically, that a
service token can never clear a human gate even with the scope, that revoke is
real, and that every action is audited."""

import pytest

from hallpass import (
    AdminScopes,
    ControlDenied,
    ControlPlane,
    InMemoryHumanGateLedger,
    InMemoryRevocationList,
    SqliteAuditLog,
    TaskQueue,
    dev_app,
)


@pytest.fixture()
def rig():
    app, token = dev_app()
    audit = SqliteAuditLog()
    queue = TaskQueue()
    revocations = InMemoryRevocationList()
    gates = InMemoryHumanGateLedger()
    cp = ControlPlane(
        verifier=app.verifier,
        audit=audit,
        queue=queue,
        revocations=revocations,
        gates=gates,
    )
    yield cp, token, audit, queue, revocations, gates
    queue.close()
    audit.close()
    app.close()


def test_queue_depth_needs_the_scope(rig):
    cp, token, _audit, queue, _rev, _gates = rig
    queue.enqueue("op", args={"n": "1"})
    queue.enqueue("op", args={"n": "2"})
    depth = cp.queue_depth(token("ops", [AdminScopes.QUEUE]))
    assert depth["pending"] == 2 and len(depth["outstanding"]) == 2
    # a caller without admin:queue is denied
    with pytest.raises(ControlDenied):
        cp.queue_depth(token("nobody", []))


def test_unauth_and_no_scope_fail_identically(rig):
    cp, token, *_ = rig
    try:
        cp.queue_depth("garbage-token")
    except ControlDenied as e1:
        unauth = str(e1)
    try:
        cp.queue_depth(token("nobody", []))
    except ControlDenied as e2:
        no_scope = str(e2)
    assert unauth == no_scope  # opaque: the surface cannot be mapped


def test_audit_tail_reads_the_trail(rig):
    cp, token, audit, _queue, _rev, _gates = rig
    from hallpass.audit import AuditEvent

    audit.record(AuditEvent(subject="alice", action="call_tool", decision="allow"))
    audit.record(AuditEvent(subject="bob", action="call_tool", decision="deny"))
    events = cp.audit_tail(token("ops", [AdminScopes.AUDIT]), limit=10)
    # newest first; includes the two we wrote (plus the control_audit_tail record)
    subjects = [e.subject for e in events]
    assert "alice" in subjects and "bob" in subjects
    # filtering passes through
    denies = cp.audit_tail(token("ops", [AdminScopes.AUDIT]), decision="deny")
    assert all(e.decision == "deny" for e in denies)
    with pytest.raises(ControlDenied):
        cp.audit_tail(token("nobody", []))


def test_revoke_is_real_and_audited(rig):
    cp, token, audit, _queue, revocations, _gates = rig
    cp.revoke_agent(token("ops", [AdminScopes.REVOKE]), "agent-7", reason="compromised")
    assert revocations.is_revoked("agent-7")
    assert cp.revoked_agents(token("ops", [AdminScopes.REVOKE])) == ["agent-7"]
    cp.restore_agent(token("ops", [AdminScopes.REVOKE]), "agent-7")
    assert not revocations.is_revoked("agent-7")
    # the revoke was recorded in the audit trail
    recorded = audit.query(action="control_revoke")
    assert recorded and recorded[0].tool == "agent-7"
    with pytest.raises(ControlDenied):
        cp.revoke_agent(token("nobody", []), "agent-7")


def test_human_gate_needs_a_human_even_with_the_scope(rig):
    cp, token, _audit, _queue, _rev, gates = rig
    gates.require("deploy-prod", reason="irreversible")
    assert cp.pending_gates(token("ops", [AdminScopes.GATE])) == ["deploy-prod"]
    # a SERVICE token holding admin:gate still cannot clear a human gate
    with pytest.raises(ControlDenied):
        cp.decide_gate(
            token("bot", [AdminScopes.GATE], service=True), "deploy-prod", approved=True
        )
    assert cp.pending_gates(token("ops", [AdminScopes.GATE])) == ["deploy-prod"]
    # a human with the scope clears it
    status = cp.decide_gate(
        token("alice", [AdminScopes.GATE]), "deploy-prod", approved=True
    )
    assert status == "approved"
    assert cp.pending_gates(token("ops", [AdminScopes.GATE])) == []


def test_unknown_gate_denies_opaquely(rig):
    cp, token, *_ = rig
    with pytest.raises(ControlDenied):
        cp.decide_gate(token("alice", [AdminScopes.GATE]), "no-such-gate")


def test_unwired_subsystem_denies_like_a_missing_scope():
    """A capability whose subsystem isn't wired denies the same opaque way as a
    missing scope -- the caller can't tell 'not configured' from 'not for you'."""
    app, token = dev_app()
    cp = ControlPlane(verifier=app.verifier)  # nothing wired
    with pytest.raises(ControlDenied):
        cp.queue_depth(token("ops", [AdminScopes.QUEUE]))  # has the scope, no queue
    app.close()
