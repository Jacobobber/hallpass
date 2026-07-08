"""One orchestrator driving a worker over hallpass, end to end, in one process.

Run it (core install only, no extras):  python examples/orchestrator.py

The orchestrator and the worker are each a Principal on one authorized A2A
channel; the task and its result are FLEX messages. In a real deployment the
worker would run in its own process (or its own agent) and poll `run_once` on a
timer; the channel's durability means it can come and go without losing work.
"""

from hallpass import A2ABus, ChannelPolicy, Orchestrator, Principal, Worker


def main() -> None:
    bus = A2ABus()
    bus.declare_channel("work", ChannelPolicy())  # open channel for the demo

    worker = Worker(bus, Principal("resizer", frozenset()), "work")

    @worker.handle("resize")
    def resize(task):
        # a real handler would call a hallpass tool here; keep it pure for the demo
        return {"status": "done", "width": task.args["width"]}

    orchestrator = Orchestrator(bus, Principal("orchestrator", frozenset()), "work")

    task_ids = [
        orchestrator.dispatch("resizer", "resize", args={"width": w}, note=f"image {i}")
        for i, w in enumerate(("640", "1024", "2048"))
    ]

    worker.run_once()  # the worker picks up and answers everything addressed to it

    for task_id, result in orchestrator.gather(task_ids).items():
        print(f"{task_id}: ok={result.ok} {result.fields} (from {result.worker})")


if __name__ == "__main__":
    main()
