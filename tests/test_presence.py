"""Presence / live roster on an A2A channel. Presence is soft state gated by
the same scopes as the messages: announcing is a write (needs post scope),
reading the roster needs read scope, and a subject that stops heartbeating
ages off. Each test names the property it pins."""

import time

import pytest

from hallpass import A2ABus, ChannelDenied, ChannelPolicy, Principal


def principal(subject, *scopes):
    return Principal(subject=subject, scopes=frozenset(scopes))


@pytest.fixture()
def bus():
    b = A2ABus()
    b.declare_channel(
        "build",
        ChannelPolicy(
            post_scopes=frozenset({"build:write"}),
            read_scopes=frozenset({"build:read"}),
        ),
    )
    yield b
    b.close()


def test_announce_then_on_roster(bus):
    worker = principal("worker-1", "build:write", "build:read")
    bus.announce(worker, "build")
    assert bus.roster(worker, "build") == ["worker-1"]


def test_roster_sorted_and_deduped(bus):
    a = principal("alice", "build:write", "build:read")
    b = principal("bob", "build:write", "build:read")
    bus.announce(b, "build")
    bus.announce(a, "build")
    bus.announce(b, "build")  # re-announce is idempotent, not a duplicate
    assert bus.roster(a, "build") == ["alice", "bob"]


def test_announce_needs_post_scope(bus):
    """Asserting presence is a write: a read-only principal cannot claim a
    seat it could not post from."""
    reader = principal("watcher", "build:read")
    with pytest.raises(ChannelDenied):
        bus.announce(reader, "build")


def test_roster_needs_read_scope(bus):
    stranger = principal("stranger")
    with pytest.raises(ChannelDenied):
        bus.roster(stranger, "build")


def test_roster_denial_is_opaque_for_unknown_channel(bus):
    """Who-is-here must not leak channel existence any more than the messages
    do: an unknown channel and a declared-but-unauthorized one fail the same
    way -- same exception, same message shape for the same name."""
    p = principal("worker-1", "build:read")
    with pytest.raises(ChannelDenied) as e1:
        bus.roster(p, "ghost")
    bus.declare_channel("ghost", ChannelPolicy(read_scopes=frozenset({"x:read"})))
    with pytest.raises(ChannelDenied) as e2:
        bus.roster(p, "ghost")
    assert str(e1.value) == str(e2.value)


def test_stale_presence_ages_off(bus):
    """A subject that stops heartbeating drops off the roster once its last
    announce is older than the window. Presence is never a durable grant."""
    worker = principal("worker-1", "build:write", "build:read")
    bus.announce(worker, "build")
    time.sleep(0.05)
    assert bus.roster(worker, "build", within=0.01) == []
    assert bus.roster(worker, "build", within=60.0) == ["worker-1"]


def test_re_announce_refreshes_heartbeat(bus):
    """A fresh announce brings a subject that had aged off back onto a narrow
    window -- the heartbeat is what keeps a seat, not the first announce."""
    worker = principal("worker-1", "build:write", "build:read")
    bus.announce(worker, "build")
    time.sleep(0.05)
    assert bus.roster(worker, "build", within=0.01) == []
    bus.announce(worker, "build")
    assert bus.roster(worker, "build", within=0.01) == ["worker-1"]


def test_presence_is_per_channel(bus):
    """A seat on one channel is not a seat on another."""
    bus.declare_channel(
        "ops",
        ChannelPolicy(
            post_scopes=frozenset({"ops:write"}),
            read_scopes=frozenset({"ops:read"}),
        ),
    )
    worker = principal("worker-1", "build:write", "build:read", "ops:read")
    bus.announce(worker, "build")
    assert bus.roster(worker, "build") == ["worker-1"]
    assert bus.roster(worker, "ops") == []


def test_presence_decision_is_audited():
    from hallpass import InMemoryAuditLog

    audit = InMemoryAuditLog()
    bus = A2ABus(audit=audit)
    bus.declare_channel(
        "build",
        ChannelPolicy(
            post_scopes=frozenset({"build:write"}),
            read_scopes=frozenset({"build:read"}),
        ),
    )
    worker = principal("worker-1", "build:write", "build:read")
    bus.announce(worker, "build")
    bus.roster(worker, "build")
    actions = {e.action for e in audit.events()}
    assert "a2a_announce" in actions
    assert "a2a_roster" in actions
    bus.close()
