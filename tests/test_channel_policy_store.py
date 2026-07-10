"""Shared A2A channel-policy store: channel authorization can live in a store
two buses share, instead of a per-process dict each replica must re-declare.
Each test names the property it pins."""

import pytest

from hallpass import (
    A2ABus,
    ChannelDenied,
    ChannelPolicy,
    InMemoryChannelPolicyStore,
    Principal,
    SqliteChannelPolicyStore,
)


def principal(subject, *scopes):
    return Principal(subject=subject, scopes=frozenset(scopes))


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryChannelPolicyStore()
    else:
        s = SqliteChannelPolicyStore(path=str(tmp_path / "policies.db"))
        yield s
        s.close()


def test_declare_get_channels(store):
    store.declare(
        "build",
        ChannelPolicy(post_scopes=frozenset({"w"}), read_scopes=frozenset({"r"})),
    )
    store.declare("ops", ChannelPolicy())
    got = store.get("build")
    assert got.post_scopes == frozenset({"w"}) and got.read_scopes == frozenset({"r"})
    assert store.get("missing") is None
    assert store.channels() == ["build", "ops"]  # sorted


def test_redeclare_replaces(store):
    store.declare("build", ChannelPolicy(post_scopes=frozenset({"a"})))
    store.declare("build", ChannelPolicy(post_scopes=frozenset({"b"})))
    assert store.get("build").post_scopes == frozenset({"b"})


def test_default_bus_still_works_unchanged():
    """A bus with no policies= arg behaves exactly as before (in-memory)."""
    bus = A2ABus()
    bus.declare_channel(
        "build",
        ChannelPolicy(
            post_scopes=frozenset({"build:write"}),
            read_scopes=frozenset({"build:read"}),
        ),
    )
    writer = principal("orch", "build:write")
    reader = principal("worker", "build:read")
    bus.post(writer, "build", "hello")
    assert [m.body for m in bus.catch_up(reader, "build")] == ["hello"]
    assert bus.channels == ["build"]
    bus.close()


def test_two_buses_share_a_policy_store(tmp_path):
    """The point of Phase 3: a channel declared once in a shared store is
    authorized identically on another bus (another replica) without re-declaring
    -- and messaging works across the two."""
    policies = SqliteChannelPolicyStore(path=str(tmp_path / "pol.db"))
    msg_path = str(tmp_path / "msgs.db")  # shared message DB too, for the round-trip
    bus_a = A2ABus(path=msg_path, policies=policies)
    bus_b = A2ABus(path=msg_path, policies=policies)

    # Replica A declares the channel; replica B never calls declare_channel.
    bus_a.declare_channel(
        "build",
        ChannelPolicy(
            post_scopes=frozenset({"build:write"}),
            read_scopes=frozenset({"build:read"}),
        ),
    )
    assert bus_b.channels == ["build"]  # B sees it via the shared store

    bus_a.post(principal("orch", "build:write"), "build", "task")
    got = bus_b.catch_up(principal("worker", "build:read"), "build")  # B authorizes it
    assert [m.body for m in got] == ["task"]
    bus_a.close()
    bus_b.close()
    policies.close()


def test_shared_store_still_denies_the_unscoped(tmp_path):
    policies = SqliteChannelPolicyStore(path=str(tmp_path / "pol.db"))
    bus = A2ABus(policies=policies)
    bus.declare_channel("secret", ChannelPolicy(read_scopes=frozenset({"secret:read"})))
    with pytest.raises(ChannelDenied):
        bus.catch_up(principal("nobody"), "secret")
    bus.close()
    policies.close()


def test_sqlite_policies_durable(tmp_path):
    path = str(tmp_path / "pol.db")
    s = SqliteChannelPolicyStore(path=path)
    s.declare("build", ChannelPolicy(post_scopes=frozenset({"w"})))
    s.close()
    reopened = SqliteChannelPolicyStore(path=path)
    assert reopened.get("build").post_scopes == frozenset({"w"})
    reopened.close()
