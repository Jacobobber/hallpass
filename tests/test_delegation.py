"""Delegation: a bounded, expiring, scope-narrowing grant from one principal to
another. Run over both stores with an injected clock. Each test names the
property it pins."""

import pytest

from hallpass import (
    DelegationError,
    InMemoryDelegationLedger,
    SqliteDelegationLedger,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(params=["memory", "sqlite"])
def ledger_clock(request, tmp_path):
    clock = _Clock()
    if request.param == "memory":
        yield InMemoryDelegationLedger(now=clock), clock
    else:
        led = SqliteDelegationLedger(path=str(tmp_path / "d.db"), now=clock)
        yield led, clock
        led.close()


def test_delegate_within_own_scopes(ledger_clock):
    led, _ = ledger_clock
    led.delegate(
        "orchestrator",
        "worker",
        ["github:read"],
        grantor_scopes=["github:read", "github:write"],
    )
    assert led.active_scopes("worker") == frozenset({"github:read"})


def test_over_delegation_is_refused(ledger_clock):
    """A grantor cannot delegate a scope it does not itself hold -- authority
    only narrows down the chain."""
    led, _ = ledger_clock
    with pytest.raises(
        DelegationError, match=r"cannot delegate scopes \['admin:all'\]"
    ):
        led.delegate(
            "orchestrator",
            "worker",
            ["github:read", "admin:all"],
            grantor_scopes=["github:read"],
        )
    assert led.active_scopes("worker") == frozenset()  # nothing recorded


def test_delegation_expires(ledger_clock):
    led, clock = ledger_clock
    led.delegate(
        "orchestrator",
        "worker",
        ["github:read"],
        grantor_scopes=["github:read"],
        ttl_seconds=60.0,
    )
    assert led.active_scopes("worker") == frozenset({"github:read"})
    clock.t += 61.0  # past the TTL
    assert led.active_scopes("worker") == frozenset()  # aged off
    assert led.granted("worker") == []


def test_no_ttl_means_standing(ledger_clock):
    led, clock = ledger_clock
    led.delegate("a", "worker", ["x:y"], grantor_scopes=["x:y"])  # no ttl
    clock.t += 10_000.0
    assert led.active_scopes("worker") == frozenset({"x:y"})  # still active


def test_active_scopes_unions_across_grantors(ledger_clock):
    led, _ = ledger_clock
    led.delegate("a", "worker", ["read:x"], grantor_scopes=["read:x"])
    led.delegate("b", "worker", ["write:y"], grantor_scopes=["write:y"])
    assert led.active_scopes("worker") == frozenset({"read:x", "write:y"})
    assert [d.grantor for d in led.granted("worker")] == ["a", "b"]  # sorted


def test_revoke(ledger_clock):
    led, _ = ledger_clock
    led.delegate("a", "worker", ["x:y"], grantor_scopes=["x:y"])
    assert led.revoke("a", "worker") is True
    assert led.active_scopes("worker") == frozenset()
    assert led.revoke("a", "worker") is False


def test_redelegate_replaces(ledger_clock):
    led, _ = ledger_clock
    led.delegate("a", "worker", ["x:y"], grantor_scopes=["x:y", "z:w"])
    led.delegate("a", "worker", ["z:w"], grantor_scopes=["x:y", "z:w"])
    assert led.active_scopes("worker") == frozenset({"z:w"})  # replaced, not merged


def test_sqlite_delegation_is_durable(tmp_path):
    clock = _Clock()
    path = str(tmp_path / "d.db")
    led = SqliteDelegationLedger(path=path, now=clock)
    led.delegate("a", "worker", ["x:y"], grantor_scopes=["x:y"], ttl_seconds=100.0)
    led.close()
    reopened = SqliteDelegationLedger(path=path, now=clock)
    assert reopened.active_scopes("worker") == frozenset({"x:y"})
    reopened.close()
