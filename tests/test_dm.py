"""Direct messages between two agents, as an auth-native private channel: a
DM is one A2A channel whose policy is a single scope only the two parties hold,
so privacy is the same scope gate as every other channel. Each test names the
property it pins."""

import pytest

from hallpass import (
    A2ABus,
    ChannelDenied,
    Principal,
    direct_channel,
    open_dm,
)


def principal(subject, *scopes):
    return Principal(subject=subject, scopes=frozenset(scopes))


def test_derivation_is_order_independent():
    """The unordered pair {a, b} always resolves to the same channel and
    scope, whichever order it is given in."""
    ab = direct_channel("alice", "bob")
    ba = direct_channel("bob", "alice")
    assert ab == ba
    assert ab.parties == ("alice", "bob")  # sorted
    assert ab.name == ab.scope  # one tag drives both


def test_distinct_pairs_do_not_collide():
    assert direct_channel("alice", "bob").name != direct_channel("alice", "carol").name
    # NUL-separated so ("ab","c") and ("a","bc") stay distinct
    assert direct_channel("ab", "c").name != direct_channel("a", "bc").name


def test_self_dm_is_rejected():
    with pytest.raises(ValueError):
        direct_channel("alice", "alice")


def test_the_two_parties_can_talk():
    bus = A2ABus()
    dc = open_dm(bus, "alice", "bob")
    alice = principal("alice", dc.scope)
    bob = principal("bob", dc.scope)
    bus.post(alice, dc.name, "hey bob")
    got = bus.catch_up(bob, dc.name)
    assert [m.body for m in got] == ["hey bob"]
    assert got[0].sender == "alice"
    bus.close()


def test_a_third_party_cannot_read_even_knowing_the_name():
    """Privacy is the scope gate: a stranger who learns the channel name still
    lacks the pair scope, and the bus denies it -- opaquely."""
    bus = A2ABus()
    dc = open_dm(bus, "alice", "bob")
    bus.post(principal("alice", dc.scope), dc.name, "secret")
    eve = principal("eve", "eve:read")  # has the name, not the scope
    with pytest.raises(ChannelDenied):
        bus.catch_up(eve, dc.name)
    bus.close()


def test_a_third_party_cannot_post():
    bus = A2ABus()
    dc = open_dm(bus, "alice", "bob")
    with pytest.raises(ChannelDenied):
        bus.post(principal("eve", "eve:write"), dc.name, "spoof")
    bus.close()


def test_open_dm_is_idempotent():
    """Re-opening the same pair does not disturb existing messages."""
    bus = A2ABus()
    dc = open_dm(bus, "alice", "bob")
    alice = principal("alice", dc.scope)
    bob = principal("bob", dc.scope)
    bus.post(alice, dc.name, "first")
    again = open_dm(bus, "bob", "alice")
    assert again == dc
    bus.post(alice, dc.name, "second")
    got = bus.catch_up(bob, dc.name)
    assert [m.body for m in got] == ["first", "second"]
    bus.close()


def test_different_pairs_are_isolated():
    """A scope for one DM does not open another."""
    bus = A2ABus()
    ab = open_dm(bus, "alice", "bob")
    ac = open_dm(bus, "alice", "carol")
    alice_ab_only = principal("alice", ab.scope)  # not carol's scope
    bus.post(principal("carol", ac.scope), ac.name, "for alice+carol")
    with pytest.raises(ChannelDenied):
        bus.catch_up(alice_ab_only, ac.name)
    bus.close()


def test_presence_works_on_a_dm():
    """A DM is an ordinary channel, so the roster gates to the pair too."""
    bus = A2ABus()
    dc = open_dm(bus, "alice", "bob")
    alice = principal("alice", dc.scope)
    bus.announce(alice, dc.name)
    assert bus.roster(alice, dc.name) == ["alice"]
    bus.close()
