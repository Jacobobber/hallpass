"""Spawning scoped agents: the Team mints each agent a token carrying only its
own scopes (the harness IS the boundary), passes it plus the task and channel by
environment, and launches it through a Spawner. What matters: each agent gets a
distinct, scope-limited token; the provisioning round-trips into the spawned
process via AgentContext.from_env; and the default subprocess spawner actually
hands a real child process that provisioning and can be stopped."""

import sys

import pytest

from hallpass import AgentContext, AgentSpec, SubprocessSpawner, Team
from hallpass.agents import ENV_CHANNEL, ENV_NAME, ENV_TASK, ENV_TOKEN


class FakeHandle:
    def __init__(self, name):
        self.name = name
        self._alive = True

    def alive(self):
        return self._alive

    def terminate(self):
        self._alive = False


class FakeSpawner:
    def __init__(self):
        self.spawned = []

    def spawn(self, spec, env):
        self.spawned.append((spec, env))
        return FakeHandle(spec.name)


def _mint(subject, scopes):
    # a stand-in token that encodes who it is for + its scopes, so a test can
    # confirm the agent was granted only its own harness
    return subject + "|" + ",".join(sorted(scopes))


def test_from_env_reads_provisioning():
    ctx = AgentContext.from_env(
        {ENV_NAME: "a", ENV_TOKEN: "tok", ENV_TASK: "do x", ENV_CHANNEL: "work"}
    )
    assert (ctx.name, ctx.token, ctx.task, ctx.channel) == ("a", "tok", "do x", "work")


def test_from_env_missing_raises():
    with pytest.raises(KeyError):
        AgentContext.from_env({ENV_NAME: "a"})  # not under a spawner


def test_team_spawn_mints_scoped_token_and_builds_env():
    fake = FakeSpawner()
    team = Team(mint=_mint, spawner=fake, channel="work")
    team.spawn(
        AgentSpec("reviewer", scopes=frozenset({"github:read"}), task="review 42")
    )
    _, env = fake.spawned[0]
    assert env[ENV_NAME] == "reviewer"
    assert env[ENV_TOKEN] == "reviewer|github:read"  # only its own scope
    assert env[ENV_TASK] == "review 42"
    assert env[ENV_CHANNEL] == "work"


def test_different_agents_get_different_harnesses():
    fake = FakeSpawner()
    team = Team(mint=_mint, spawner=fake, channel="work")
    team.spawn(AgentSpec("reviewer", scopes=frozenset({"github:read"})))
    team.spawn(AgentSpec("messenger", scopes=frozenset({"slack:write"})))
    tokens = [env[ENV_TOKEN] for _, env in fake.spawned]
    # each agent's token carries only its harness -- reviewer can't reach Slack
    assert tokens == ["reviewer|github:read", "messenger|slack:write"]


def test_alive_and_shutdown():
    fake = FakeSpawner()
    team = Team(mint=_mint, spawner=fake, channel="work")
    team.spawn(AgentSpec("a"))
    team.spawn(AgentSpec("b"))
    assert set(team.alive()) == {"a", "b"}
    team.shutdown()
    assert team.alive() == []


def test_subprocess_spawner_passes_provisioning(tmp_path):
    out = tmp_path / "got.txt"
    prog = (
        "from hallpass import AgentContext; c = AgentContext.from_env(); "
        f"open({out.as_posix()!r}, 'w', encoding='utf-8')"
        ".write('|'.join([c.name, c.task, c.channel, c.token]))"
    )
    team = Team(
        mint=_mint,
        spawner=SubprocessSpawner([sys.executable, "-c", prog]),
        channel="work",
    )
    handle = team.spawn(
        AgentSpec("reviewer", scopes=frozenset({"github:read"}), task="do it")
    )
    handle.process.wait(timeout=60)
    assert out.read_text(encoding="utf-8") == "reviewer|do it|work|reviewer|github:read"


def test_subprocess_handle_alive_then_terminate():
    team = Team(
        mint=_mint,
        spawner=SubprocessSpawner(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ),
        channel="w",
    )
    handle = team.spawn(AgentSpec("sleeper"))
    assert handle.alive() is True
    handle.terminate()
    handle.process.wait(timeout=15)
    assert handle.alive() is False
