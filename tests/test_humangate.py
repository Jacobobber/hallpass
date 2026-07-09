"""Human gates: an action held pending until a HUMAN decides; a service
principal can never clear it. Run over both stores. Each test names the property
it pins."""

import pytest

from hallpass import (
    HumanGateError,
    InMemoryHumanGateLedger,
    Principal,
    SqliteHumanGateLedger,
)


def human(subject):
    return Principal(subject=subject, scopes=frozenset(), kind="user")


def service(subject):
    return Principal(subject=subject, scopes=frozenset(), kind="service")


@pytest.fixture(params=["memory", "sqlite"])
def gates(request, tmp_path):
    if request.param == "memory":
        yield InMemoryHumanGateLedger()
    else:
        led = SqliteHumanGateLedger(path=str(tmp_path / "gates.db"))
        yield led
        led.close()


def test_open_gate_is_pending(gates):
    gates.require("widen-scope:bot-1", reason="grant admin")
    assert gates.pending() == ["widen-scope:bot-1"]
    assert gates.cleared("widen-scope:bot-1") is False


def test_human_can_approve(gates):
    gates.require("g1", reason="destructive op")
    decided = gates.decide("g1", human("alice"), approved=True, note="ok")
    assert decided.status == "approved" and decided.decided_by == "alice"
    assert gates.cleared("g1") is True
    assert gates.pending() == []


def test_human_can_deny(gates):
    gates.require("g1")
    gates.decide("g1", human("alice"), approved=False)
    assert gates.cleared("g1") is False
    assert gates.get("g1").status == "denied"


def test_service_principal_cannot_decide(gates):
    """An agent (service principal) can never clear a human gate -- the whole
    point. The gate stays pending."""
    gates.require("g1")
    with pytest.raises(HumanGateError, match="may not decide human gate"):
        gates.decide("g1", service("agent-1"), approved=True)
    assert gates.pending() == ["g1"]  # still awaiting a human
    assert gates.cleared("g1") is False


def test_decision_is_final(gates):
    gates.require("g1")
    gates.decide("g1", human("alice"), approved=True)
    with pytest.raises(HumanGateError, match="already decided"):
        gates.decide("g1", human("bob"), approved=False)
    assert gates.get("g1").decided_by == "alice"  # unchanged


def test_deciding_unknown_gate_raises(gates):
    with pytest.raises(KeyError, match="no human gate 'nope'"):
        gates.decide("nope", human("alice"), approved=True)


def test_require_is_idempotent_while_pending(gates):
    gates.require("g1", reason="first")
    gates.require("g1", reason="second")
    assert gates.pending() == ["g1"]  # one gate, not two


def test_sqlite_pending_gate_survives_restart(tmp_path):
    """A gate opened before a restart is still pending after it -- a pending
    human decision is never silently lost."""
    path = str(tmp_path / "g.db")
    led = SqliteHumanGateLedger(path=path)
    led.require("release-prod", reason="irreversible")
    led.close()
    reopened = SqliteHumanGateLedger(path=path)
    assert reopened.pending() == ["release-prod"]
    reopened.decide("release-prod", human("alice"), approved=True)
    reopened.close()
    again = SqliteHumanGateLedger(path=path)
    assert again.cleared("release-prod") is True  # decision persisted too
    again.close()
