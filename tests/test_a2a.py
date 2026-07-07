"""A2A channels apply hallpass's identity and scope model to agent-to-agent
messaging, with durable at-least-once delivery. Each test names the thing
that must hold: authorization on post and read, opaque denial (channel
existence does not leak), durable at-least-once delivery, per-principal
cursors, forward-only acks, and audited decisions."""

import pytest

from hallpass import A2ABus, ChannelDenied, ChannelPolicy, InMemoryAuditLog, Principal


def principal(subject, *scopes):
    return Principal(subject=subject, scopes=frozenset(scopes))


@pytest.fixture()
def bus():
    b = A2ABus()
    b.declare_channel(
        "build",
        ChannelPolicy(
            post_scopes=frozenset({"build:write"}),
            read_scopes=frozenset({"build:read"}),
        ),
    )
    yield b
    b.close()


def test_post_and_read_roundtrip(bus):
    writer = principal("orchestrator", "build:write")
    reader = principal("worker", "build:read")
    bus.post(writer, "build", "task: resize batch-7")
    got = bus.catch_up(reader, "build")
    assert [m.body for m in got] == ["task: resize batch-7"]
    assert got[0].sender == "orchestrator"


def test_post_denied_without_scope(bus):
    with pytest.raises(ChannelDenied):
        bus.post(principal("worker", "build:read"), "build", "sneaky")


def test_read_denied_without_scope(bus):
    bus.post(principal("orchestrator", "build:write"), "build", "x")
    with pytest.raises(ChannelDenied):
        bus.catch_up(principal("stranger"), "build")


def test_undeclared_channel_is_indistinguishable_from_unauthorized(bus):
    """A caller must not be able to tell 'no such channel' from 'not for
    you' -- both must fail closed with the same opaque message."""
    p = principal("worker", "build:read")
    try:
        bus.catch_up(p, "does-not-exist")
    except ChannelDenied as e1:
        unknown_msg = str(e1)
    # A declared channel the caller lacks read scope for:
    bus.declare_channel("secret", ChannelPolicy(read_scopes=frozenset({"secret:read"})))
    try:
        bus.catch_up(p, "secret")
    except ChannelDenied as e2:
        unauthorized_msg = str(e2)
    assert unknown_msg == "no channel named 'does-not-exist'"
    assert unauthorized_msg == "no channel named 'secret'"
    # neither names a scope
    assert "scope" not in unknown_msg and "scope" not in unauthorized_msg


def test_ack_only_after_handling_redelivers(bus):
    writer = principal("orchestrator", "build:write")
    reader = principal("worker", "build:read")
    bus.post(writer, "build", "job-1")
    first = bus.catch_up(reader, "build")
    assert len(first) == 1
    # crash: no ack. A fresh read still sees it.
    second = bus.catch_up(reader, "build")
    assert [m.body for m in second] == ["job-1"]
    bus.ack(reader, "build", second[-1].seq)
    assert bus.catch_up(reader, "build") == []


def test_ack_is_forward_only(bus):
    writer = principal("orchestrator", "build:write")
    reader = principal("worker", "build:read")
    for i in range(3):
        bus.post(writer, "build", f"job-{i}")
    bus.ack(reader, "build", 3)
    bus.ack(reader, "build", 1)  # stale ack: no regression
    assert bus.catch_up(reader, "build") == []


def test_cannot_ack_beyond_head(bus):
    bus.post(principal("orchestrator", "build:write"), "build", "job-1")
    with pytest.raises(ValueError):
        bus.ack(principal("worker", "build:read"), "build", 99)


def test_cursors_are_per_principal(bus):
    writer = principal("orchestrator", "build:write")
    a = principal("worker-a", "build:read")
    b = principal("worker-b", "build:read")
    bus.post(writer, "build", "job-1")
    assert len(bus.catch_up(a, "build")) == 1
    assert len(bus.catch_up(b, "build")) == 1
    bus.ack(a, "build", 1)
    assert bus.catch_up(a, "build") == []
    assert [m.body for m in bus.catch_up(b, "build")] == ["job-1"]  # b unaffected


def test_decisions_are_audited_including_denials():
    log = InMemoryAuditLog()
    bus = A2ABus(audit=log)
    bus.declare_channel("build", ChannelPolicy(post_scopes=frozenset({"build:write"})))
    bus.post(principal("orchestrator", "build:write"), "build", "ok")
    with pytest.raises(ChannelDenied):
        bus.post(principal("nobody"), "build", "denied")
    events = log.events()
    assert any(e.action == "a2a_post" and e.decision == "allow" for e in events)
    denies = [e for e in events if e.decision == "deny"]
    assert (
        denies
        and denies[-1].reason == "not_authorized"
        and denies[-1].subject == "nobody"
    )
    bus.close()


def test_empty_policy_allows_any_authenticated_principal(bus):
    bus.declare_channel("open", ChannelPolicy())  # no scopes required
    bus.post(principal("anyone"), "open", "hello")
    assert [m.body for m in bus.catch_up(principal("another"), "open")] == ["hello"]


def test_durable_across_bus_instances(tmp_path):
    db = str(tmp_path / "a2a.sqlite3")
    policy = ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"}))
    first = A2ABus(path=db)
    first.declare_channel("c", policy)
    first.post(principal("o", "w"), "c", "persisted")
    first.close()

    second = A2ABus(path=db)
    second.declare_channel("c", policy)  # policy is in-memory; messages are durable
    got = second.catch_up(principal("worker", "r"), "c")
    assert [m.body for m in got] == ["persisted"]
    second.close()
