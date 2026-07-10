"""Shared cross-replica backends (Redis) for idempotency and rate limiting.
Tested against a fake client (no real Redis), so these run in the default suite.
Each test names the property it pins, especially the cross-replica one the
in-memory stores lose."""

import pytest

from hallpass import (
    RateLimited,
    RedisIdempotencyStore,
    RedisRateLimiter,
)


class _FakeRedis:
    """A minimal in-memory stand-in for the redis client surface these backends
    use. One instance shared by two store objects models two replicas talking to
    the same Redis."""

    def __init__(self):
        self.kv: dict[str, str] = {}

    def get(self, name):
        return self.kv.get(name)

    def set(self, name, value, *, ex=None):
        self.kv[name] = value

    def incr(self, name):
        self.kv[name] = str(int(self.kv.get(name, "0")) + 1)
        return int(self.kv[name])

    def expire(self, name, seconds):
        pass


# -- idempotency -----------------------------------------------------------


def test_put_then_get_roundtrips():
    store = RedisIdempotencyStore(_FakeRedis())
    assert store.get("alice", "resize", "k1") == (False, None)
    store.put("alice", "resize", "k1", {"status": "done", "n": 3})
    assert store.get("alice", "resize", "k1") == (True, {"status": "done", "n": 3})


def test_keys_scoped_per_subject_and_tool():
    store = RedisIdempotencyStore(_FakeRedis())
    store.put("alice", "resize", "k1", "alice-result")
    assert store.get("bob", "resize", "k1") == (False, None)  # different subject
    assert store.get("alice", "delete", "k1") == (False, None)  # different tool


def test_result_is_shared_across_replicas():
    """The whole point: a result recorded by one replica is visible to another
    (same Redis), so a retry landing on a different replica does not re-run."""
    shared = _FakeRedis()
    replica_a = RedisIdempotencyStore(shared)
    replica_b = RedisIdempotencyStore(shared)
    replica_a.put("alice", "charge", "idem-1", {"charged": True})
    assert replica_b.get("alice", "charge", "idem-1") == (True, {"charged": True})


# -- rate limiting ---------------------------------------------------------


def test_allows_up_to_the_limit_then_raises():
    clock = {"t": 1000.0}
    rl = RedisRateLimiter(3, 60.0, _FakeRedis(), now=lambda: clock["t"])
    rl.check("alice")
    rl.check("alice")
    rl.check("alice")  # 3rd ok
    with pytest.raises(RateLimited):
        rl.check("alice")  # 4th over budget


def test_budget_is_per_subject():
    clock = {"t": 1000.0}
    rl = RedisRateLimiter(1, 60.0, _FakeRedis(), now=lambda: clock["t"])
    rl.check("alice")
    rl.check("bob")  # bob has his own budget
    with pytest.raises(RateLimited):
        rl.check("alice")


def test_budget_is_shared_across_replicas():
    """Two replicas sharing one Redis enforce ONE budget -- not N x the cap,
    the silent failure of the per-process limiter behind a load balancer."""
    shared = _FakeRedis()
    clock = {"t": 1000.0}
    a = RedisRateLimiter(2, 60.0, shared, now=lambda: clock["t"])
    b = RedisRateLimiter(2, 60.0, shared, now=lambda: clock["t"])
    a.check("alice")
    b.check("alice")  # 2nd, via the other replica
    with pytest.raises(RateLimited):
        a.check("alice")  # 3rd anywhere is over the shared budget


def test_window_rolls_over():
    clock = {"t": 1000.0}
    rl = RedisRateLimiter(1, 60.0, _FakeRedis(), now=lambda: clock["t"])
    rl.check("alice")
    with pytest.raises(RateLimited):
        rl.check("alice")
    clock["t"] += 61.0  # next window
    rl.check("alice")  # budget refreshed


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        RedisRateLimiter(0, 60.0, _FakeRedis())
    with pytest.raises(ValueError):
        RedisRateLimiter(1, 0.0, _FakeRedis())


def test_satisfy_the_protocols():
    """The Redis backends are drop-in for the in-memory ones: same protocols."""
    from hallpass import IdempotencyStore, RateLimiter

    idem: IdempotencyStore = RedisIdempotencyStore(_FakeRedis())
    rl: RateLimiter = RedisRateLimiter(5, 60.0, _FakeRedis())
    assert hasattr(idem, "get") and hasattr(idem, "put")
    assert hasattr(rl, "check")
