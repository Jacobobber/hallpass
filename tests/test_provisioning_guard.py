"""The provisioning guard: a spawned agent must be its own scoped service
identity, checked before launch. Each test names the misprovisioning path it
refuses -- and asserts a rejected agent is never launched. hallpass already
makes cross-subject credential reads impossible; the guard closes the one
remaining gap, an operator minting a token that isn't the agent's own."""

import pytest

from hallpass import (
    AgentSpec,
    ProvisioningError,
    ProvisioningGuard,
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
    """Records what it was asked to launch, so a test can assert that a
    rejected agent never reaches the spawner."""

    def __init__(self):
        self.spawned = []

    def spawn(self, spec, env):
        self.spawned.append((spec.name, env))
        return _FakeHandle(spec.name)


@pytest.fixture()
def app_token():
    app, token = dev_app()
    return app, token


def _team(app, token, *, mint, guard=True, **guard_kwargs):
    spawner = _RecordingSpawner()
    g = ProvisioningGuard(app.verifier, **guard_kwargs) if guard else None
    team = Team(mint=mint, spawner=spawner, channel="work", guard=g)
    return team, spawner


SPEC = AgentSpec("reviewer", scopes=frozenset({"github:read"}), task="review PR 42")


def test_own_scoped_service_identity_launches(app_token):
    app, token = app_token
    team, spawner = _team(app, token, mint=lambda n, s: token(n, s, service=True))
    team.spawn(SPEC)
    assert [name for name, _ in spawner.spawned] == ["reviewer"]


def test_human_impersonation_is_refused(app_token):
    """A minter that signs a subject other than the agent's own name is the
    path by which an agent could act as the human. Refused, and not launched."""
    app, token = app_token
    team, spawner = _team(
        app, token, mint=lambda n, s: token("human-operator", s, service=True)
    )
    with pytest.raises(ProvisioningError, match="does not match agent name"):
        team.spawn(SPEC)
    assert spawner.spawned == []


def test_user_kind_token_is_refused(app_token):
    """Spawned agents must be service identities; a user-kind token is refused
    (unless the operator opts out deliberately)."""
    app, token = app_token
    team, spawner = _team(app, token, mint=lambda n, s: token(n, s))  # service=False
    with pytest.raises(ProvisioningError, match="user-kind token"):
        team.spawn(SPEC)
    assert spawner.spawned == []


def test_scope_widening_is_refused(app_token):
    """The minter must grant exactly the harness scopes; a widened grant is
    refused, so an agent can't be handed capability beyond its declared harness."""
    app, token = app_token
    team, spawner = _team(
        app,
        token,
        mint=lambda n, s: token(n, frozenset(s) | {"admin:everything"}, service=True),
    )
    with pytest.raises(ProvisioningError, match="do not match the declared harness"):
        team.spawn(SPEC)
    assert spawner.spawned == []


def test_unverifiable_token_is_refused(app_token):
    """A token the server's own verifier would reject never launches an agent."""
    app, token = app_token
    team, spawner = _team(app, token, mint=lambda n, s: "not.a.valid.jwt")
    with pytest.raises(ProvisioningError, match="does not verify"):
        team.spawn(SPEC)
    assert spawner.spawned == []


def test_require_service_false_opts_out(app_token):
    """Setting require_service=False accepts a user-kind token (subject and
    scopes are still checked) -- a deliberate opt-out, not the default."""
    app, token = app_token
    team, spawner = _team(
        app, token, mint=lambda n, s: token(n, s), require_service=False
    )
    team.spawn(SPEC)
    assert [name for name, _ in spawner.spawned] == ["reviewer"]


def test_no_guard_is_backward_compatible(app_token):
    """A Team without a guard keeps the pre-guard behavior: it launches
    whatever the minter returns (the guard is opt-in, additive)."""
    app, token = app_token
    team, spawner = _team(app, token, mint=lambda n, s: token(n, s), guard=False)
    team.spawn(SPEC)
    assert [name for name, _ in spawner.spawned] == ["reviewer"]


def test_guard_check_is_usable_standalone(app_token):
    """The guard is a plain object usable outside Team (e.g. in a provisioning
    service)."""
    app, token = app_token
    guard = ProvisioningGuard(app.verifier)
    guard.check(SPEC, token("reviewer", ["github:read"], service=True))  # no raise
    with pytest.raises(ProvisioningError):
        guard.check(SPEC, token("someone-else", ["github:read"], service=True))
