"""A durable, queryable audit sink is what lets an operator answer "what did
user X do", "what got denied", and "which calls were slow" after the fact. The
properties that matter: every decision (allow AND deny) is recorded, filters
AND together and return newest-first, timing rides on completed calls, and the
trail survives across store instances (a restart)."""

from cryptography.fernet import Fernet

from hallpass import (
    Hallpass,
    SqliteAuditLog,
    ToolKit,
)
from hallpass.audit import AuditEvent
from hallpass.identity import StaticJwks, TokenVerifier

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def test_record_and_query_by_subject():
    log = SqliteAuditLog()
    log.record(AuditEvent("alice", "call_tool", "allow", tool="t1"))
    log.record(AuditEvent("bob", "call_tool", "allow", tool="t2"))
    got = log.query(subject="alice")
    assert [e.subject for e in got] == ["alice"]
    assert got[0].tool == "t1"
    log.close()


def test_filters_and_together_newest_first():
    log = SqliteAuditLog()
    log.record(AuditEvent("alice", "call_tool", "allow", tool="t1", at=1.0))
    log.record(AuditEvent("alice", "call_tool", "deny", tool="t2", at=2.0))
    log.record(AuditEvent("alice", "call_tool", "deny", tool="t3", at=3.0))
    denials = log.query(subject="alice", decision="deny")
    assert [e.tool for e in denials] == ["t3", "t2"]  # newest first
    log.close()


def test_since_and_limit():
    log = SqliteAuditLog()
    for i in range(5):
        log.record(AuditEvent("u", "call_tool", "allow", tool=f"t{i}", at=float(i)))
    assert len(log.query(since=3.0)) == 2  # at 3.0 and 4.0
    assert len(log.query(limit=2)) == 2
    log.close()


def test_survives_across_instances(tmp_path):
    path = str(tmp_path / "audit.sqlite3")
    a = SqliteAuditLog(path=path)
    a.record(AuditEvent("alice", "call_tool", "allow", tool="t1"))
    a.close()
    b = SqliteAuditLog(path=path)
    assert [e.subject for e in b.query()] == ["alice"]
    b.close()


def test_end_to_end_records_allow_deny_and_duration(keypair):
    kit = ToolKit("demo")

    @kit.tool(scopes=["demo:read"], name="ping", description="pong")
    def ping(ctx, **kwargs):
        return {"pong": True}

    log = SqliteAuditLog()
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    app = Hallpass(verifier=verifier, vault=_vault(), audit=log)
    app.add_connector(kit)

    # an allowed call records with a duration
    app.call_tool(mint(keypair, sub="alice", scope="demo:read"), "ping", {})
    # a denied call (missing scope) is recorded too
    try:
        app.call_tool(mint(keypair, sub="bob", scope=""), "ping", {})
    except Exception:
        pass

    allows = log.query(decision="allow", action="call_tool")
    denies = log.query(decision="deny", action="call_tool")
    assert allows and allows[0].subject == "alice"
    assert allows[0].duration_ms is not None and allows[0].duration_ms >= 0.0
    assert (
        denies and denies[0].subject == "bob" and denies[0].reason == "not_authorized"
    )
    app.close()
    log.close()


def _vault():
    from hallpass import CredentialVault

    return CredentialVault(Fernet.generate_key())
