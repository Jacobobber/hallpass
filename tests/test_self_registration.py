"""Boot-time self-registration: a spawned agent knows its own scopes (carried
in its context) and announces itself onto its channel, so an orchestrator's
roster sees it live and a Router can route to it -- without the orchestrator
pre-configuring it. Each test names the property it pins."""

from hallpass import (
    A2ABus,
    AgentContext,
    ChannelPolicy,
    Principal,
    Router,
    Team,
    join_channel,
)
from hallpass.agents import ENV_SCOPES


class _Handle:
    def __init__(self, name):
        self.name = name

    def alive(self):
        return True

    def terminate(self):
        pass


class _CapturingSpawner:
    def __init__(self):
        self.env = None

    def spawn(self, spec, env):
        self.env = env
        return _Handle(spec.name)


def test_spawn_passes_scopes_in_env():
    from hallpass import AgentSpec, dev_app

    _, token = dev_app()
    spawner = _CapturingSpawner()
    team = Team(mint=lambda n, s: token(n, s), spawner=spawner, channel="work")
    team.spawn(AgentSpec("reviewer", scopes=frozenset({"github:read", "github:write"})))
    assert spawner.env[ENV_SCOPES] == "github:read github:write"  # space-joined, sorted


def test_context_reconstructs_its_scoped_principal():
    env = {
        "HALLPASS_AGENT_NAME": "reviewer",
        "HALLPASS_AGENT_TOKEN": "tok",
        "HALLPASS_AGENT_TASK": "review",
        "HALLPASS_AGENT_CHANNEL": "work",
        ENV_SCOPES: "github:read github:write",
    }
    ctx = AgentContext.from_env(env)
    assert ctx.scopes == frozenset({"github:read", "github:write"})
    p = ctx.principal()
    assert p.subject == "reviewer" and p.scopes == ctx.scopes


def test_scopes_default_empty_when_env_absent_backward_compat():
    """An agent launched by a pre-scopes spawner still loads (empty scopes),
    so the new env var is additive."""
    env = {
        "HALLPASS_AGENT_NAME": "old",
        "HALLPASS_AGENT_TOKEN": "tok",
        "HALLPASS_AGENT_TASK": "t",
        "HALLPASS_AGENT_CHANNEL": "work",
    }
    ctx = AgentContext.from_env(env)
    assert ctx.scopes == frozenset()


def test_join_channel_announces_presence():
    bus = A2ABus()
    bus.declare_channel(
        "work",
        ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"w"})),
    )
    ctx = AgentContext(
        name="reviewer", token="tok", task="t", channel="work", scopes=frozenset({"w"})
    )
    me = join_channel(bus, ctx)
    assert me.subject == "reviewer"
    # an orchestrator (holding the read scope) sees the agent live on the roster
    orch = Principal("orch", frozenset({"w"}))
    assert bus.roster(orch, "work") == ["reviewer"]
    bus.close()


def test_join_channel_registers_with_router():
    bus = A2ABus()
    bus.declare_channel("work", ChannelPolicy())
    router = Router()
    ctx = AgentContext(
        name="gpu-worker",
        token="tok",
        task="resize",
        channel="work",
        scopes=frozenset({"img:write", "img:read"}),
    )
    join_channel(bus, ctx, router=router)
    # the agent registered itself; the router can now route matching work to it
    assert router.route(frozenset({"img:write"})) == "gpu-worker"
    assert router.candidates(frozenset({"img:read"})) == ["gpu-worker"]
    bus.close()


def test_end_to_end_spawn_then_self_register():
    """The scopes a Team spawns with survive into the agent's context and drive
    a correct self-registration -- spawn and self-register compose."""
    from hallpass import AgentSpec, dev_app

    _, token = dev_app()
    spawner = _CapturingSpawner()
    team = Team(mint=lambda n, s: token(n, s), spawner=spawner, channel="work")
    team.spawn(AgentSpec("gpu-worker", scopes=frozenset({"img:write"})))

    # the spawned process would call AgentContext.from_env() on exactly this env
    ctx = AgentContext.from_env(spawner.env)
    bus = A2ABus()
    bus.declare_channel("work", ChannelPolicy())
    router = Router()
    join_channel(bus, ctx, router=router)
    assert router.route(frozenset({"img:write"})) == "gpu-worker"
    bus.close()
