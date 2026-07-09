"""Consent ledger: the in-memory one is thread-safe (the reference HTTP server
is threaded), and the SQLite one is durable (a grant survives a restart, a
revoke is persistent) and concurrency-safe. Each test names the property."""

import threading

import pytest

from hallpass import (
    Consent,
    InMemoryConsentLedger,
    SqliteConsentLedger,
)


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    if request.param == "memory":
        yield InMemoryConsentLedger()
    else:
        led = SqliteConsentLedger(path=str(tmp_path / "consent.db"))
        yield led
        led.close()


def test_grant_get_roundtrip(ledger):
    ledger.grant("alice", "github", ["repo", "read:user"], at=100.0)
    c = ledger.get("alice", "github")
    assert isinstance(c, Consent)
    assert c.subject == "alice" and c.service == "github"
    assert c.scopes == ("repo", "read:user") and c.granted_at == 100.0


def test_get_absent_is_none(ledger):
    assert ledger.get("alice", "nope") is None


def test_grant_replaces(ledger):
    ledger.grant("alice", "github", ["a"], at=1.0)
    ledger.grant("alice", "github", ["a", "b"], at=2.0)
    c = ledger.get("alice", "github")
    assert c.scopes == ("a", "b") and c.granted_at == 2.0


def test_list_sorted_and_scoped_to_subject(ledger):
    ledger.grant("alice", "slack", [], at=1.0)
    ledger.grant("alice", "github", ["repo"], at=1.0)
    ledger.grant("bob", "notion", [], at=1.0)
    services = [c.service for c in ledger.list("alice")]
    assert services == ["github", "slack"]  # sorted, no bob


def test_revoke(ledger):
    ledger.grant("alice", "github", ["repo"], at=1.0)
    assert ledger.revoke("alice", "github") is True
    assert ledger.get("alice", "github") is None
    assert ledger.revoke("alice", "github") is False  # already gone


def test_empty_scopes_roundtrip(ledger):
    ledger.grant("alice", "svc", [], at=5.0)
    assert ledger.get("alice", "svc").scopes == ()


def test_sqlite_consent_is_durable(tmp_path):
    """A grant survives a fresh ledger on the same file -- the whole point of
    the durable backing over the in-memory one."""
    path = str(tmp_path / "c.db")
    led = SqliteConsentLedger(path=path)
    led.grant("alice", "github", ["repo"], at=42.0)
    led.close()

    reopened = SqliteConsentLedger(path=path)
    c = reopened.get("alice", "github")
    assert c is not None and c.scopes == ("repo",) and c.granted_at == 42.0
    # and a revoke is persistent too
    reopened.revoke("alice", "github")
    reopened.close()
    again = SqliteConsentLedger(path=path)
    assert again.get("alice", "github") is None
    again.close()


def test_inmemory_concurrent_grants_are_not_lost():
    """Many threads granting distinct services concurrently: the lock means no
    grant is dropped by a racing write."""
    led = InMemoryConsentLedger()
    n = 200

    def worker(i):
        led.grant("alice", f"svc{i}", [f"scope{i}"], at=float(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(led.list("alice")) == n


def test_sqlite_concurrent_grants_are_not_lost(tmp_path):
    led = SqliteConsentLedger(path=str(tmp_path / "c.db"))
    n = 200

    def worker(i):
        led.grant("alice", f"svc{i}", [f"scope{i}"], at=float(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(led.list("alice")) == n
    led.close()
