"""The program a spawned agent runs. The Team launches this in its own process
with a scoped token, a task, and a channel; it picks that up, self-registers on
the channel, does its task, and reports. In a real agent this is where a model
loop would run, calling hallpass tools that its token's scopes allow. Here it
just reports, to keep the demo dependency-free.

Not meant to be run directly; `examples/spawn_agents.py` launches it.
"""

import os

from hallpass import A2ABus, AgentContext, ChannelPolicy, flex, join_channel


def main() -> None:
    ctx = AgentContext.from_env()  # name, token, task, channel, and its own scopes
    bus = A2ABus(path=os.environ["HALLPASS_CHANNEL_DB"])
    # Channel policies are per-process config; the SQLite file carries only the
    # messages. Each participant declares the channels it uses.
    bus.declare_channel(ctx.channel, ChannelPolicy())
    # Boot-time self-registration: announce presence so the orchestrator's
    # roster sees this agent live. `join_channel` builds the agent's own
    # principal from its context (name + granted scopes) and returns it.
    me = join_channel(bus, ctx)
    bus.post(
        me,
        ctx.channel,
        flex.encode(
            flex.Message(kind="result", refs=(ctx.name,), note=f"handled: {ctx.task}")
        ),
    )
    bus.close()


if __name__ == "__main__":
    main()
