"""Direct messages between two agents, as an auth-native private channel.

A DM is not a new transport. It is one ``A2ABus`` channel whose policy is
satisfied by a single scope that only the two parties hold, so privacy is the
same scope gate that guards every other channel rather than a bolted-on access
list. Derivation is pure and deterministic:

    direct_channel(a, b) -> DirectChannel(name, scope, parties)

The name and the guarding scope are one stable, order-independent tag over the
two subjects, so ``direct_channel(a, b) == direct_channel(b, a)`` and the same
pair always resolves to the same channel (idempotent to re-open). A third party
who somehow learns the channel name still cannot post or read it: it lacks the
scope, and the bus denies it opaquely, exactly as for any other channel.

``open_dm(bus, a, b)`` declares the channel on the bus and returns the
descriptor; the caller then mints each of the two principals a token carrying
``descriptor.scope`` (typically alongside their other scopes). From there it is
an ordinary channel: ``bus.post`` / ``bus.catch_up`` / ``bus.announce`` all work
and stay gated to the pair.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .a2a import A2ABus, ChannelPolicy

__all__ = ["DirectChannel", "direct_channel", "open_dm"]


@dataclass(frozen=True)
class DirectChannel:
    """A resolved direct channel between two subjects. ``name`` is the channel
    to pass to the bus; ``scope`` is the single scope both parties must hold to
    post and read; ``parties`` are the two subjects, sorted."""

    name: str
    scope: str
    parties: tuple[str, str]

    def policy(self) -> ChannelPolicy:
        """The channel policy that gates this DM to its two parties: the pair
        scope is required to both post and read."""
        return ChannelPolicy(
            post_scopes=frozenset({self.scope}),
            read_scopes=frozenset({self.scope}),
        )


def _tag(a: str, b: str) -> tuple[str, str, str]:
    lo, hi = sorted((a, b))
    # A stable, order-independent digest of the unordered pair. The NUL
    # separator keeps ("ab", "c") and ("a", "bc") from colliding. Truncated to
    # 16 hex chars: 64 bits is ample to keep distinct pairs distinct, and the
    # tag is not a secret (the scope, not the name, is what gates access).
    digest = hashlib.sha256(f"{lo}\x00{hi}".encode()).hexdigest()[:16]
    return lo, hi, digest


def direct_channel(a: str, b: str) -> DirectChannel:
    """Resolve the direct channel for the unordered pair ``{a, b}``. Pure and
    deterministic: order-independent, and the same pair always yields the same
    channel name and scope. Raises if the two subjects are the same (a DM needs
    two distinct parties)."""
    if a == b:
        raise ValueError("a direct channel needs two distinct subjects")
    lo, hi, digest = _tag(a, b)
    return DirectChannel(name=f"dm:{digest}", scope=f"dm:{digest}", parties=(lo, hi))


def open_dm(bus: A2ABus, a: str, b: str) -> DirectChannel:
    """Declare the direct channel for ``{a, b}`` on ``bus`` and return its
    descriptor. Idempotent: re-declaring the same pair replaces the policy with
    an identical one and leaves any existing messages untouched. The caller is
    responsible for minting each party a token that carries
    ``descriptor.scope`` -- that scope is what makes the channel theirs."""
    dc = direct_channel(a, b)
    bus.declare_channel(dc.name, dc.policy())
    return dc
