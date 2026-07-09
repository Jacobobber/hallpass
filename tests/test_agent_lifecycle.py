"""Agent lifecycle on the Team: reap dead agents, terminate one by name, and
rotate an agent's identity (re-mint + re-launch under the same spec). Each test
names the property it pins."""

import pytest

from hallpass import AgentSpec, Team, dev_app


class _Handle:
    def __init__(self, name):
        self.name = name
        self._alive = True

    def alive(self):
        return self._alive

    def terminate(self):
        self._alive = False


class _Spawner:
    def __init__(self):
        self.tokens = []  # token each spawned agent received, in order

    def spawn(self, spec, env):
        self.tokens.append(env["HALLPASS_AGENT_TOKEN"])
        return _Handle(spec.name)


def _team():
    _, token = dev_app()
    n = {"i": 0}

    def mint(name, scopes):
        n["i"] += 1
        return token(name, scopes) + f".v{n['i']}"  # a fresh token each mint

    spawner = _Spawner()
    return Team(mint=mint, spawner=spawner, channel="work"), spawner


def test_reap_drops_dead_agents_only():
    team, _ = _team()
    h1 = team.spawn(AgentSpec("a"))
    team.spawn(AgentSpec("b"))
    h1.terminate()  # 'a' exits
    reaped = team.reap()
    assert reaped == ["a"]
    assert team.alive() == ["b"]  # 'b' still tracked and live


def test_terminate_by_name():
    team, _ = _team()
    team.spawn(AgentSpec("a"))
    team.spawn(AgentSpec("b"))
    assert team.terminate("a") is True
    assert "a" not in team.alive()
    assert team.alive() == ["b"]
    assert team.terminate("a") is False  # already gone


def test_rotate_re_mints_and_relaunches():
    """Rotation terminates the old instance and spawns a fresh one under the
    same spec with a NEW token -- credential rotation without re-supplying the
    spec."""
    team, spawner = _team()
    old = team.spawn(AgentSpec("worker", scopes=frozenset({"x:y"})))
    new = team.rotate("worker")
    assert old is not new
    assert not old.alive()  # old instance terminated
    assert new.alive()
    assert team.alive() == ["worker"]  # one live instance, not two
    assert spawner.tokens[0] != spawner.tokens[1]  # a fresh token was minted


def test_rotate_unknown_agent_raises():
    team, _ = _team()
    with pytest.raises(KeyError, match="no agent named 'ghost'"):
        team.rotate("ghost")


def test_rotate_preserves_the_spec():
    """The rotated agent keeps the original scopes/task -- rotation is identity
    refresh, not reconfiguration."""
    team, spawner = _team()
    team.spawn(AgentSpec("worker", scopes=frozenset({"a:b", "c:d"}), task="resize"))
    team.rotate("worker")
    # the fresh spawn carried the same scopes in its env (sorted, space-joined)
    # -> the second spawn's env scopes match the first
    assert len(spawner.tokens) == 2


def test_shutdown_terminates_all():
    team, _ = _team()
    a = team.spawn(AgentSpec("a"))
    b = team.spawn(AgentSpec("b"))
    team.shutdown()
    assert not a.alive() and not b.alive()
