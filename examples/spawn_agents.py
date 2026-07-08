"""Spawn two agents with different harnesses and different tasks, then collect
what they report on a shared channel.

Run it (core install only):  python examples/spawn_agents.py

Each agent runs in its own process with a token scoped to just its harness: the
reviewer's token carries github:read, the messenger's carries slack:write, and
nothing else. The Team mints those tokens and launches the processes; the agents
coordinate back over one file-backed A2A channel.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

from hallpass import (
    A2ABus,
    AgentSpec,
    ChannelPolicy,
    Principal,
    SubprocessSpawner,
    Team,
    dev_app,
    flex,
)


def main() -> None:
    db = str(Path(tempfile.gettempdir()) / "hallpass-team-demo.sqlite3")
    if os.path.exists(db):
        os.remove(db)

    bus = A2ABus(path=db)
    bus.declare_channel("work", ChannelPolicy())  # open channel for the demo
    _, token = dev_app()  # dev_app's minter signs scoped tokens for the agents

    agent_program = str(Path(__file__).parent / "spawned_agent.py")
    team = Team(
        mint=lambda name, scopes: token(name, scopes),
        spawner=SubprocessSpawner([sys.executable, agent_program]),
        channel="work",
    )

    team.spawn(
        AgentSpec("reviewer", scopes=frozenset({"github:read"}), task="review PR 42"),
        extra_env={"HALLPASS_CHANNEL_DB": db},
    )
    team.spawn(
        AgentSpec("messenger", scopes=frozenset({"slack:write"}), task="post to #eng"),
        extra_env={"HALLPASS_CHANNEL_DB": db},
    )

    while team.alive():  # wait for both agents to finish and report
        time.sleep(0.05)

    orchestrator = Principal("orchestrator", frozenset())
    for msg in bus.catch_up(orchestrator, "work"):
        print(f"{msg.sender}: {flex.parse(msg.body).note}")
    bus.close()


if __name__ == "__main__":
    main()
