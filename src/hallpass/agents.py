"""Spawn scoped agents and hand each a harness, a task, and a channel.

This is where the orchestrator stops dispatching to workers that happen to
exist and starts *creating* them, each with a different harness and a different
job, all coordinating over the A2A channels. The point that makes it hallpass
and not just another agent-teams clone:

    Every spawned agent is a scoped identity, not a trusted one.

The orchestrator mints each agent a token carrying only that agent's scopes, so
a "reviewer" can reach the GitHub tools and nothing else and a "messenger" can
reach Slack and nothing else, enforced at call time by the same core and
audited. Isolation between agents is the auth layer, not a promise.

hallpass stays a substrate, not a model: it provisions the identity and harness,
passes them plus the task and channel to the new process by environment, and
launches it through a pluggable ``Spawner``. What thinks inside the process is
yours. The default ``SubprocessSpawner`` runs a command you give it; the agent
program calls ``AgentContext.from_env()`` to pick up what it was handed.

    # orchestrator side
    team = Team(mint=my_minter, spawner=SubprocessSpawner(["python", "agent.py"]),
                channel="work")
    team.spawn(AgentSpec("reviewer", scopes=frozenset({"github:read"}), task="review #42"))

    # inside agent.py
    ctx = AgentContext.from_env()      # ctx.token, ctx.task, ctx.channel, ctx.name
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "AgentSpec",
    "AgentContext",
    "AgentHandle",
    "Spawner",
    "SubprocessSpawner",
    "Team",
    "ENV_NAME",
    "ENV_TOKEN",
    "ENV_TASK",
    "ENV_CHANNEL",
]

# The contract between the spawner and the spawned agent program. Both sides
# agree on these names; the agent reads them via AgentContext.from_env().
ENV_NAME = "HALLPASS_AGENT_NAME"
ENV_TOKEN = "HALLPASS_AGENT_TOKEN"  # noqa: S105 - env var NAME, not a secret
ENV_TASK = "HALLPASS_AGENT_TASK"
ENV_CHANNEL = "HALLPASS_AGENT_CHANNEL"


@dataclass(frozen=True)
class AgentSpec:
    """What to spawn: a name (also the agent's identity subject), the scopes
    that define its harness (which tools it may call, enforced by the core),
    a task, and metadata. The channel is supplied by the Team."""

    name: str
    scopes: frozenset[str] = frozenset()
    task: str = ""
    harness: str = ""  # optional label for the capability preset used


@dataclass(frozen=True)
class AgentContext:
    """What a spawned agent picks up about itself. Its `token` carries only the
    scopes its harness was granted; the core gates everything else."""

    name: str
    token: str
    task: str
    channel: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentContext:
        """Read the provisioning the spawner passed in. Call this first thing
        inside a spawned agent program."""
        source = env if env is not None else os.environ
        missing = [
            n for n in (ENV_NAME, ENV_TOKEN, ENV_TASK, ENV_CHANNEL) if n not in source
        ]
        if missing:
            raise KeyError(
                "not running under a hallpass spawner; missing " + ", ".join(missing)
            )
        return cls(
            name=source[ENV_NAME],
            token=source[ENV_TOKEN],
            task=source[ENV_TASK],
            channel=source[ENV_CHANNEL],
        )


class AgentHandle(Protocol):
    """A running agent, enough to check on it and stop it."""

    name: str

    def alive(self) -> bool: ...

    def terminate(self) -> None: ...


class Spawner(Protocol):
    """Launch one provisioned agent. ``env`` holds the ``HALLPASS_AGENT_*``
    variables plus whatever the caller's environment already had."""

    def spawn(self, spec: AgentSpec, env: dict[str, str]) -> AgentHandle: ...


@dataclass
class _SubprocessHandle:
    name: str
    process: subprocess.Popen[bytes]

    def alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self) -> None:
        if self.alive():
            self.process.terminate()


class SubprocessSpawner:
    """Launch each agent as a subprocess of a fixed command, with the
    provisioning passed by environment. The same command runs for every agent;
    the command reads ``AgentContext.from_env()`` to learn who it is and what to
    do. Nothing about the model runtime is assumed."""

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("command must be a non-empty argv list")
        self._command = command

    def spawn(self, spec: AgentSpec, env: dict[str, str]) -> AgentHandle:
        process = subprocess.Popen(self._command, env={**os.environ, **env})
        return _SubprocessHandle(name=spec.name, process=process)


class Team:
    """Spawns scoped agents onto one channel and tracks them.

    ``mint`` turns a (subject, scopes) pair into a token the hallpass server
    will accept -- `dev_app`'s minter for local use, your IdP's
    client-credentials flow in production. Each spawned agent's token carries
    only its own scopes, so the harness *is* the capability boundary."""

    def __init__(
        self,
        *,
        mint: Callable[[str, frozenset[str]], str],
        spawner: Spawner,
        channel: str,
    ) -> None:
        self._mint = mint
        self._spawner = spawner
        self._channel = channel
        self._handles: list[AgentHandle] = []

    def spawn(
        self, spec: AgentSpec, *, extra_env: Mapping[str, str] | None = None
    ) -> AgentHandle:
        """Provision the agent (mint its scoped token) and launch it. The agent
        receives its name, token, task, and channel by environment."""
        token = self._mint(spec.name, spec.scopes)
        env = {
            ENV_NAME: spec.name,
            ENV_TOKEN: token,
            ENV_TASK: spec.task,
            ENV_CHANNEL: self._channel,
            **(dict(extra_env) if extra_env else {}),
        }
        handle = self._spawner.spawn(spec, env)
        self._handles.append(handle)
        return handle

    @property
    def agents(self) -> list[AgentHandle]:
        return list(self._handles)

    def alive(self) -> list[str]:
        """Names of agents still running."""
        return [h.name for h in self._handles if h.alive()]

    def shutdown(self) -> None:
        """Terminate every agent this team spawned."""
        for handle in self._handles:
            handle.terminate()
