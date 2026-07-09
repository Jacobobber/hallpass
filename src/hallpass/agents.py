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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .identity import Principal, TokenVerifier, VerificationError

if TYPE_CHECKING:
    from .a2a import A2ABus
    from .orchestrator import Router

__all__ = [
    "AgentSpec",
    "AgentContext",
    "AgentHandle",
    "Harness",
    "HarnessRegistry",
    "ProvisioningError",
    "ProvisioningGuard",
    "Spawner",
    "SubprocessSpawner",
    "Team",
    "join_channel",
    "ENV_NAME",
    "ENV_TOKEN",
    "ENV_TASK",
    "ENV_CHANNEL",
    "ENV_SCOPES",
]

# The contract between the spawner and the spawned agent program. Both sides
# agree on these names; the agent reads them via AgentContext.from_env().
ENV_NAME = "HALLPASS_AGENT_NAME"
ENV_TOKEN = "HALLPASS_AGENT_TOKEN"  # noqa: S105 - env var NAME, not a secret
ENV_TASK = "HALLPASS_AGENT_TASK"
ENV_CHANNEL = "HALLPASS_AGENT_CHANNEL"
ENV_SCOPES = "HALLPASS_AGENT_SCOPES"  # space-joined; the agent's granted scopes


@dataclass(frozen=True)
class AgentSpec:
    """What to spawn: a name (also the agent's identity subject), the scopes
    that define its harness (which tools it may call, enforced by the core),
    a task, and metadata. The channel is supplied by the Team."""

    name: str
    scopes: frozenset[str] = frozenset()
    task: str = ""
    harness: str = ""  # names a Harness preset in the Team's registry (optional)


@dataclass(frozen=True)
class Harness:
    """A named capability preset for a *type* of agent: the maximum scope set an
    agent of this type may ever be granted. It makes ``AgentSpec.harness`` more
    than a label — the Team resolves the name to this preset and refuses to
    spawn an agent whose requested scopes exceed it, so "reviewer" or
    "messenger" means one thing, declared once, everywhere it is spawned."""

    name: str
    scopes: frozenset[str] = frozenset()


class HarnessRegistry:
    """The declared harness types for a fleet. A ``Team`` consults it to resolve
    ``AgentSpec.harness`` to its preset and bound the agent's scopes to it."""

    def __init__(self, harnesses: Iterable[Harness] = ()) -> None:
        self._by_name: dict[str, Harness] = {}
        for harness in harnesses:
            self.register(harness)

    def register(self, harness: Harness) -> None:
        """Add or replace a harness type."""
        self._by_name[harness.name] = harness

    def get(self, name: str) -> Harness | None:
        return self._by_name.get(name)

    def preset(self, name: str) -> frozenset[str]:
        """The scope preset for ``name``. Raises ``KeyError`` if the harness
        type was never declared — a spec asking for an unknown harness is a
        misconfiguration, not a silent empty grant."""
        harness = self._by_name.get(name)
        if harness is None:
            raise KeyError(
                f"no harness named {name!r} is registered; declare it in the "
                "HarnessRegistry before spawning an agent that uses it"
            )
        return harness.scopes

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)


@dataclass(frozen=True)
class AgentContext:
    """What a spawned agent picks up about itself. Its `token` carries only the
    scopes its harness was granted; the core gates everything else. `scopes` is
    the same set as a convenience so the agent can build its own `Principal`
    (e.g. to join its channel) without decoding the token."""

    name: str
    token: str
    task: str
    channel: str
    scopes: frozenset[str] = frozenset()

    def principal(self) -> Principal:
        """This agent's own principal (its name + granted scopes), for local
        use of the bus/channels. Authoritative auth is still the token, verified
        at a channel/tool service; this is the in-process identity."""
        return Principal(subject=self.name, scopes=self.scopes)

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
        raw_scopes = source.get(ENV_SCOPES, "")
        return cls(
            name=source[ENV_NAME],
            token=source[ENV_TOKEN],
            task=source[ENV_TASK],
            channel=source[ENV_CHANNEL],
            scopes=frozenset(raw_scopes.split()),
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
        harnesses: HarnessRegistry | None = None,
    ) -> None:
        self._mint = mint
        self._spawner = spawner
        self._channel = channel
        # When set, each minted token is verified against the guard before the
        # agent is launched; a token that is not this agent's own scoped service
        # identity raises ProvisioningError and nothing is spawned.
        self._guard = guard
        # When set, a spec's `harness` names a preset here, and the agent's
        # requested scopes must stay within it -- so an agent type's capability
        # ceiling is declared once, not re-typed per spawn.
        self._harnesses = harnesses
        self._handles: list[AgentHandle] = []
        # Latest spec per agent name, so an agent can be rotated (re-minted +
        # re-launched under the same spec) without the caller re-supplying it.
        self._specs: dict[str, AgentSpec] = {}

    def spawn(
        self, spec: AgentSpec, *, extra_env: Mapping[str, str] | None = None
    ) -> AgentHandle:
        """Provision the agent (mint its scoped token) and launch it. The agent
        receives its name, token, task, and channel by environment. If the spec
        names a ``harness`` and a ``HarnessRegistry`` was given, its scopes must
        stay within that preset; and if a ``ProvisioningGuard`` was given, the
        minted token is checked before launch. Either bound raises
        ``ProvisioningError`` and nothing is spawned."""
        self._bound_to_harness(spec)
        token = self._mint(spec.name, spec.scopes)
        if self._guard is not None:
            self._guard.check(spec, token)
        env = {
            ENV_NAME: spec.name,
            ENV_TOKEN: token,
            ENV_TASK: spec.task,
            ENV_CHANNEL: self._channel,
            ENV_SCOPES: " ".join(sorted(spec.scopes)),
            **(dict(extra_env) if extra_env else {}),
        }
        handle = self._spawner.spawn(spec, env)
        self._handles.append(handle)
        self._specs[spec.name] = spec
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

    def terminate(self, name: str) -> bool:
        """Terminate a specific agent by name and stop tracking it. Returns True
        if one was running. Revoking the agent's *credentials* is separate (kill
        its IdP client / `OAuthConnect.disconnect`); this stops the process."""
        matched = [h for h in self._handles if h.name == name]
        for handle in matched:
            handle.terminate()
        self._handles = [h for h in self._handles if h.name != name]
        return bool(matched)

    def reap(self) -> list[str]:
        """Drop agents that have already exited from tracking and return their
        names. Nothing is terminated -- these are already dead; this just keeps
        the roster of tracked handles from growing without bound."""
        dead = [h.name for h in self._handles if not h.alive()]
        self._handles = [h for h in self._handles if h.alive()]
        return dead

    def rotate(
        self, name: str, *, extra_env: Mapping[str, str] | None = None
    ) -> AgentHandle:
        """Rotate an agent's identity: terminate the running instance and spawn
        a fresh one under the same spec, which re-mints its token (a new
        credential) and re-runs the harness bound and provisioning guard. Raises
        ``KeyError`` if the agent was never spawned by this team."""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(
                f"no agent named {name!r} was spawned by this team; nothing to rotate"
            )
        self.terminate(name)
        return self.spawn(spec, extra_env=extra_env)

    def _bound_to_harness(self, spec: AgentSpec) -> None:
        """Refuse, before minting, to spawn an agent whose requested scopes
        exceed its declared harness type. No-op when the spec names no harness
        or the team has no registry (harness stays an optional label)."""
        if not spec.harness or self._harnesses is None:
            return
        try:
            preset = self._harnesses.preset(spec.harness)
        except KeyError as exc:
            raise ProvisioningError(str(exc)) from None
        extra = frozenset(spec.scopes) - preset
        if extra:
            raise ProvisioningError(
                f"agent {spec.name!r} requests scopes {sorted(extra)} outside its "
                f"harness {spec.harness!r} (allowed: {sorted(preset)}); widen the "
                "harness or narrow the spec"
            )


def join_channel(
    bus: A2ABus,
    ctx: AgentContext,
    *,
    router: Router | None = None,
) -> Principal:
    """Boot-time self-registration for a spawned agent. Announce presence on the
    agent's channel so an orchestrator's roster sees it live, and -- if given an
    in-process ``Router`` -- register the agent's capability so it can be routed
    work, without the orchestrator having to pre-configure it. The channel must
    already be declared on ``bus``; returns the agent's principal for reuse.

    The agent presents its own ``Principal`` (its name + granted scopes). In a
    single-process team the router is shared and this wires discovery directly;
    across processes the ``announce`` is the durable signal an orchestrator reads
    from the roster (a per-process ``Router`` lives with the orchestrator)."""
    me = ctx.principal()
    bus.announce(me, ctx.channel)
    if router is not None:
        router.register(ctx.name, ctx.scopes)
    return me
