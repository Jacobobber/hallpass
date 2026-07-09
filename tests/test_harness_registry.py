"""Harness presets: a harness type declares the maximum scope set an agent of
that type may hold, once, and the Team bounds every spawn to it. Each test
names the property it pins."""

import pytest

from hallpass import (
    AgentSpec,
    Harness,
    HarnessRegistry,
    ProvisioningError,
    Team,
    dev_app,
)


class _FakeHandle:
    def __init__(self, name):
        self.name = name

    def alive(self):
        return True

    def terminate(self):
        pass


class _RecordingSpawner:
    def __init__(self):
        self.spawned = []

    def spawn(self, spec, env):
        self.spawned.append(spec.name)
        return _FakeHandle(spec.name)


REGISTRY = HarnessRegistry(
    [
        Harness("reviewer", frozenset({"github:read", "github:write"})),
        Harness("messenger", frozenset({"slack:write"})),
    ]
)


def test_registry_lookup():
    assert REGISTRY.preset("reviewer") == frozenset({"github:read", "github:write"})
    assert REGISTRY.get("messenger").scopes == frozenset({"slack:write"})
    assert REGISTRY.names == ["messenger", "reviewer"]


def test_unknown_harness_raises_keyerror():
    with pytest.raises(KeyError, match="no harness named"):
        REGISTRY.preset("nope")


def _team():
    app, token = dev_app()
    spawner = _RecordingSpawner()
    team = Team(
        mint=lambda n, s: token(n, s),
        spawner=spawner,
        channel="work",
        harnesses=REGISTRY,
    )
    return team, spawner


def test_scopes_within_harness_spawn():
    team, spawner = _team()
    team.spawn(AgentSpec("r1", scopes=frozenset({"github:read"}), harness="reviewer"))
    assert spawner.spawned == ["r1"]


def test_scopes_exceeding_harness_are_refused():
    """An agent cannot be spawned with scopes beyond its harness type; the
    excess is named and nothing launches."""
    team, spawner = _team()
    with pytest.raises(ProvisioningError, match=r"outside its harness 'reviewer'"):
        team.spawn(
            AgentSpec(
                "r1", scopes=frozenset({"github:read", "admin:all"}), harness="reviewer"
            )
        )
    assert spawner.spawned == []


def test_unknown_harness_on_spawn_is_refused():
    team, spawner = _team()
    with pytest.raises(ProvisioningError, match="no harness named 'ghost'"):
        team.spawn(AgentSpec("x", scopes=frozenset(), harness="ghost"))
    assert spawner.spawned == []


def test_no_harness_label_is_unbounded_backward_compat():
    """A spec with no harness name (or a team with no registry) keeps the old
    behavior: the label is optional, not required."""
    app, token = dev_app()
    spawner = _RecordingSpawner()
    # team WITH a registry, but the spec names no harness -> unbounded
    team = Team(
        mint=lambda n, s: token(n, s),
        spawner=spawner,
        channel="work",
        harnesses=REGISTRY,
    )
    team.spawn(AgentSpec("free", scopes=frozenset({"anything:goes"})))
    assert spawner.spawned == ["free"]


def test_harness_bound_and_guard_compose():
    """The harness bound (scopes ⊆ preset, pre-mint) and the guard (minted token
    == spec, service-kind) are independent checks that both hold."""
    from hallpass import ProvisioningGuard

    app, token = dev_app()
    spawner = _RecordingSpawner()
    team = Team(
        mint=lambda n, s: token(n, s, service=True),
        spawner=spawner,
        channel="work",
        guard=ProvisioningGuard(app.verifier),
        harnesses=REGISTRY,
    )
    team.spawn(AgentSpec("r1", scopes=frozenset({"github:write"}), harness="reviewer"))
    assert spawner.spawned == ["r1"]
