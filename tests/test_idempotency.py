"""A retried mutating tool call must not execute twice. With an idempotency
key and a store wired, a repeat of the same (subject, tool, key) returns the
first result without re-running the handler. The properties that matter: only
successful calls are remembered (a failure leaves nothing so a retry can
succeed), keys are scoped per subject and per tool (no cross-user or cross-tool
result leakage), and without a key or store the behaviour is unchanged."""

import pytest

from hallpass import InMemoryIdempotencyStore, ToolKit, build
from hallpass.identity import StaticJwks

from conftest import AUDIENCE, ISSUER, jwk_for, mint


class Counter:
    """A connector whose tool increments a per-call counter, so we can see
    exactly how many times the handler actually ran."""

    def __init__(self):
        self.calls = 0

    def kit(self):
        kit = ToolKit("ctr")

        @kit.tool(scopes=["ctr:write"], name="bump", description="increment")
        def bump(ctx, **kwargs):
            self.calls += 1
            return {"count": self.calls}

        @kit.tool(scopes=["ctr:write"], name="boom", description="always fails")
        def boom(ctx, **kwargs):
            self.calls += 1
            raise RuntimeError("downstream exploded")

        return kit


def _app(store=None):
    counter = Counter()
    app = build(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(_KP, "k1")]}),
        idempotency=store,
        connectors=[counter.kit()],
    )
    return app, counter


@pytest.fixture(autouse=True)
def _keypair(keypair):
    global _KP
    _KP = keypair
    yield


def test_repeat_with_same_key_runs_once():
    app, counter = _app(InMemoryIdempotencyStore())
    tok = mint(_KP, sub="alice", scope="ctr:write")
    first = app.call_tool(tok, "bump", {}, idempotency_key="op-1")
    second = app.call_tool(tok, "bump", {}, idempotency_key="op-1")
    assert first == {"count": 1}
    assert second == {"count": 1}  # cached, not re-run
    assert counter.calls == 1
    app.close()


def test_different_keys_run_each_time():
    app, counter = _app(InMemoryIdempotencyStore())
    tok = mint(_KP, sub="alice", scope="ctr:write")
    app.call_tool(tok, "bump", {}, idempotency_key="op-1")
    app.call_tool(tok, "bump", {}, idempotency_key="op-2")
    assert counter.calls == 2
    app.close()


def test_no_key_never_caches():
    app, counter = _app(InMemoryIdempotencyStore())
    tok = mint(_KP, sub="alice", scope="ctr:write")
    app.call_tool(tok, "bump", {})
    app.call_tool(tok, "bump", {})
    assert counter.calls == 2  # unchanged behaviour without a key
    app.close()


def test_no_store_ignores_key():
    app, counter = _app(store=None)  # key supplied but nowhere to cache
    tok = mint(_KP, sub="alice", scope="ctr:write")
    app.call_tool(tok, "bump", {}, idempotency_key="op-1")
    app.call_tool(tok, "bump", {}, idempotency_key="op-1")
    assert counter.calls == 2
    app.close()


def test_failed_call_is_not_remembered():
    app, counter = _app(InMemoryIdempotencyStore())
    tok = mint(_KP, sub="alice", scope="ctr:write")
    with pytest.raises(RuntimeError):
        app.call_tool(tok, "boom", {}, idempotency_key="op-1")
    with pytest.raises(RuntimeError):
        app.call_tool(tok, "boom", {}, idempotency_key="op-1")
    assert counter.calls == 2  # retried, because nothing was cached on failure
    app.close()


def test_key_is_scoped_per_subject():
    app, counter = _app(InMemoryIdempotencyStore())
    alice = mint(_KP, sub="alice", scope="ctr:write")
    bob = mint(_KP, sub="bob", scope="ctr:write")
    a = app.call_tool(alice, "bump", {}, idempotency_key="shared")
    b = app.call_tool(bob, "bump", {}, idempotency_key="shared")
    # bob's identical key must not return alice's result
    assert a == {"count": 1}
    assert b == {"count": 2}
    assert counter.calls == 2
    app.close()


def test_store_get_put_and_ttl():
    clock = {"t": 1000.0}
    store = InMemoryIdempotencyStore(ttl_seconds=100.0, now=lambda: clock["t"])
    assert store.get("alice", "bump", "k") == (False, None)
    store.put("alice", "bump", "k", {"count": 1})
    assert store.get("alice", "bump", "k") == (True, {"count": 1})
    clock["t"] += 200.0  # past TTL
    assert store.get("alice", "bump", "k") == (False, None)
