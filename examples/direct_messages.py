"""A private 1:1 channel between two agents, end to end, in one process.

Run it (core install only, no extras):  python examples/direct_messages.py

A DM is not a new transport -- it is one A2A channel whose policy is a single
scope that only the two parties hold. `open_dm` derives that channel from the
unordered pair and declares it; each party is minted a token carrying the pair
scope. A third party who learns the channel name still cannot touch it, because
the scope, not the name, is what gates access. Privacy is the same scope gate
that guards every other channel.
"""

from hallpass import A2ABus, Principal, direct_channel, open_dm


def main() -> None:
    bus = A2ABus()

    # Resolve + declare the DM for {alice, bob}. Order does not matter.
    dm = open_dm(bus, "alice", "bob")
    print(f"channel {dm.name} gated by scope {dm.scope!r} for {dm.parties}")

    # Each party holds only the pair scope for this DM.
    alice = Principal("alice", frozenset({dm.scope}))
    bob = Principal("bob", frozenset({dm.scope}))

    bus.post(alice, dm.name, "bob, can you take batch-7?")
    bus.post(bob, dm.name, "on it")

    for msg in bus.catch_up(alice, dm.name):
        print(f"  {msg.sender}: {msg.body}")

    # A stranger who somehow knows the channel name still lacks the scope.
    eve = Principal("eve", frozenset({"eve:read"}))
    try:
        bus.catch_up(eve, dm.name)
    except Exception as exc:  # ChannelDenied -- opaque, same as an unknown channel
        print(f"  eve is denied: {type(exc).__name__}")

    # The derivation is pure, so anyone with both subjects resolves the same
    # channel without a lookup -- open_dm just declares it on the bus.
    assert direct_channel("bob", "alice") == dm
    bus.close()


if __name__ == "__main__":
    main()
