"""A served worker agent: the reference loop hallpass gives you for the inside
of a spawned agent's process.

Run it (core install only, no extras):  python examples/reference_agent.py

hallpass runs no model loop of its own; a handler is where your model or tool
call would go. Everything around it -- claim a task, run it, report, heartbeat
so the agent stays on the live roster, stop cleanly -- is `serve_queue` /
`run_worker`, so it is not rewritten per agent. This demo serves a durable
TaskQueue; swap in `run_worker` to serve an A2A channel instead.
"""

from hallpass import A2ABus, ChannelPolicy, Principal, TaskQueue, serve_queue


def main() -> None:
    queue = TaskQueue()  # pass path=... for real cross-process durability
    for i, width in enumerate(("640", "1024", "2048")):
        queue.enqueue("resize", args={"width": width}, note=f"image {i}")

    # A live-roster seat, so an orchestrator can see this worker is up.
    bus = A2ABus()
    bus.declare_channel("fleet", ChannelPolicy())
    me = Principal("resizer-1", frozenset())

    def resize(task):
        # a real handler would call a hallpass tool (or a model) here
        return {"status": "done", "width": task.args["width"]}

    # Serve until the backlog drains (no stop / max_idle_rounds set), heartbeating
    # each pass. In a real deployment you would pass stop=... to serve forever.
    completed = serve_queue(
        queue,
        "resizer-1",
        {"resize": resize},
        heartbeat=lambda: bus.announce(me, "fleet"),
    )
    print(f"completed {completed} tasks; roster now {bus.roster(me, 'fleet')}")

    queue.close()
    bus.close()


if __name__ == "__main__":
    main()
