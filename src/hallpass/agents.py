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

from .identity import TokenVerifier, VerificationError

__all__ = [
    "AgentSpec",
    "AgentContext",
    "AgentHandle",
    "ProvisioningError",
    "ProvisioningGuard",
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


class ProvisioningError(Exception):
    """A minted agent token failed the provisioning guard: it is not a scoped
    service identity for exactly this agent. Raised BEFORE launch, so a
    misprovisioned agent never runs."""


@dataclass(frozen=True)
class ProvisioningGuard:
    """Verify, before launch, that a spawned agent is what it claims: a service
    identity, whose subject is the agent's own name, carrying exactly the
    harness scopes and no more.

    hallpass already makes an agent structurally unable to read another
    subject's credential (the vault is keyed by subject). The gap that leaves is
    provisioning: nothing stops a ``mint`` callable from signing a *human's*
    subject, or a user-kind token, or a widened scope set — and any of those
    would let a spawned agent act with an identity that is not its own. The
    guard closes that gap by checking the minted token against the same verifier
    the server uses, and refusing to launch if it is not this agent's own scoped
    service identity. It turns "the operator must provision honestly" into a
    checked invariant."""

    verifier: TokenVerifier
    require_service: bool = True

    def check(self, spec: AgentSpec, token: str) -> None:
        """Raise ``ProvisioningError`` unless ``token`` verifies as a service
        principal whose subject is ``spec.name`` and whose scopes are exactly
        ``spec.scopes``. Set ``require_service=False`` only to deliberately opt
        out of the service-kind requirement."""
        try:
            principal = self.verifier.verify(token)
        except VerificationError as exc:
            raise ProvisioningError(
                f"minted token for agent {spec.name!r} does not verify "
                f"({type(exc).__name__}); the minter must issue a token this "
                "server's verifier accepts"
            ) from None
        if principal.subject != spec.name:
            raise ProvisioningError(
                f"minted token subject {principal.subject!r} does not match agent "
                f"name {spec.name!r}: an agent must act as itself, never as "
                "another subject (this is the human-impersonation path)"
            )
        if self.require_service and not principal.is_service:
            raise ProvisioningError(
                f"agent {spec.name!r} was minted a user-kind token: spawned "
                "agents must be service identities. Configure the verifier's "
                "service_claim/service_values and mint a service token (or pass "
                "require_service=False to opt out deliberately)"
            )
        if principal.scopes != frozenset(spec.scopes):
            raise ProvisioningError(
                f"minted scopes {sorted(principal.scopes)} do not match the "
                f"declared harness {sorted(spec.scopes)} for agent {spec.name!r}: "
                "the minter must grant exactly the harness scopes, no more"
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
        guard: ProvisioningGuard | None = None,
    ) -> None:
        self._mint = mint
        self._spawner = spawner
        self._channel = channel
        # When set, each minted token is verified against the guard before the
        # agent is launched; a token that is not this agent's own scoped service
        # identity raises ProvisioningError and nothing is spawned.
        self._guard = guard
        self._handles: list[AgentHandle] = []

    def spawn(
        self, spec: AgentSpec, *, extra_env: Mapping[str, str] | None = None
    ) -> AgentHandle:
        """Provision the agent (mint its scoped token) and launch it. The agent
        receives its name, token, task, and channel by environment. If a
        ``ProvisioningGuard`` was given, the minted token is checked before
        launch and a misprovisioned agent raises ``ProvisioningError`` instead
        of starting."""
        token = self._mint(spec.name, spec.scopes)
        if self._guard is not None:
            self._guard.check(spec, token)
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
