"""Seats: durable per-(channel, role) membership with self-service rebind. Run
over both stores. Each test names the property it pins."""

import pytest

from hallpass import InMemorySeatLedger, SqliteSeatLedger


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    if request.param == "memory":
        yield InMemorySeatLedger()
    else:
        led = SqliteSeatLedger(path=str(tmp_path / "seats.db"))
        yield led
        led.close()


def test_bind_and_holder(ledger):
    ledger.bind("build", "reviewer", "alice")
    assert ledger.holder("build", "reviewer") == "alice"
    assert ledger.holder("build", "releaser") is None  # vacant


def test_one_holder_per_seat_rebind_replaces(ledger):
    """Self-service rebind: a new subject taking the seat replaces the old
    holder -- one holder per (channel, role)."""
    ledger.bind("build", "reviewer", "alice")
    ledger.bind("build", "reviewer", "bob")
    assert ledger.holder("build", "reviewer") == "bob"
    assert [s.subject for s in ledger.seats("build")] == ["bob"]  # not two


def test_unbind_vacates(ledger):
    ledger.bind("build", "reviewer", "alice")
    assert ledger.unbind("build", "reviewer") is True
    assert ledger.holder("build", "reviewer") is None
    assert ledger.unbind("build", "reviewer") is False  # already vacant


def test_seats_lists_a_channels_seats_sorted(ledger):
    ledger.bind("build", "releaser", "carol")
    ledger.bind("build", "reviewer", "alice")
    ledger.bind("ops", "oncall", "dave")  # different channel
    roles = [(s.role, s.subject) for s in ledger.seats("build")]
    assert roles == [("releaser", "carol"), ("reviewer", "alice")]  # sorted, build only


def test_held_by_spans_channels(ledger):
    ledger.bind("build", "reviewer", "alice")
    ledger.bind("ops", "oncall", "alice")
    ledger.bind("build", "releaser", "bob")
    held = [(s.channel, s.role) for s in ledger.held_by("alice")]
    assert held == [("build", "reviewer"), ("ops", "oncall")]  # sorted, alice only


def test_seats_are_per_channel(ledger):
    ledger.bind("build", "reviewer", "alice")
    assert ledger.holder("ops", "reviewer") is None  # same role, different channel


def test_sqlite_seats_are_durable(tmp_path):
    path = str(tmp_path / "s.db")
    led = SqliteSeatLedger(path=path)
    led.bind("build", "reviewer", "alice")
    led.close()
    reopened = SqliteSeatLedger(path=path)
    assert reopened.holder("build", "reviewer") == "alice"  # survived restart
    reopened.close()


def test_seat_is_durable_not_liveness():
    """A seat is not a heartbeat: it persists until explicitly rebound/unbound,
    unlike presence which ages off. (Contrast documented in seats.py.)"""
    led = InMemorySeatLedger()
    seat = led.bind("build", "reviewer", "alice")
    assert seat.subject == "alice" and seat.bound_at > 0
    # no time passing / no heartbeat -> still held
    assert led.holder("build", "reviewer") == "alice"
