"""The program a spawned agent runs. The Team launches this in its own process
with a scoped token, a task, and a channel; it picks that up, does its task, and
reports on the channel. In a real agent this is where a model loop would run,
calling hallpass tools that its token's scopes allow. Here it just reports, to
keep the demo dependency-free.

Not meant to be run directly; `examples/spawn_agents.py` launches it.
"""

import os

from hallpass import A2ABus, AgentContext, ChannelPolicy, Principal, flex


def main() -> None:
    ctx = AgentContext.from_env()  # name, token (scoped to this agent), task, channel
    bus = A2ABus(path=os.environ["HALLPASS_CHANNEL_DB"])
    # Channel policies are per-process config; the SQLite file carries only the
    # messages. Each participant declares the channels it uses.
    bus.declare_channel(ctx.channel, ChannelPolicy())
    # A real deployment verifies the token at a channel/tool service; in this
    # single-box demo the agent acts as its own principal on an open channel.
    me = Principal(ctx.name, frozenset())
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
