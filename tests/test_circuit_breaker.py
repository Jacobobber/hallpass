"""A struggling downstream should be given a rest, not hammered by a fleet of
agents retrying into it. The circuit breaker opens after a run of outages and
fails fast (without touching the downstream) until a cooldown, then a single
half-open probe decides to close or re-open. What matters: it trips only on real
outages (5xx / connection errors), not client errors (404); it fails fast while
open; it recovers via the probe; and a success resets the count. The clock is
injected, so none of this waits on real time."""

import pytest

from hallpass import (
    BreakerPolicy,
    CircuitBreakerHttpClient,
    CircuitOpen,
    ConnectorError,
)


class ScriptedHttp:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, *, headers, params, json, data=None):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _call(cb):
    return cb.request("GET", "u", headers={}, params={}, json=None)


def test_opens_after_threshold_then_fails_fast():
    inner = ScriptedHttp([ConnectorError("down", status=503)] * 3)
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=3), now=Clock()
    )
    for _ in range(3):
        with pytest.raises(ConnectorError):
            _call(cb)
    assert inner.calls == 3
    # open now: the next call fails fast without reaching the downstream
    with pytest.raises(CircuitOpen):
        _call(cb)
    assert inner.calls == 3


def test_half_open_probe_closes_on_success():
    clock = Clock()
    inner = ScriptedHttp(
        [ConnectorError("down", status=503)] * 2 + [{"ok": 1}, {"ok": 2}]
    )
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=2, reset_after=30.0), now=clock
    )
    for _ in range(2):
        with pytest.raises(ConnectorError):
            _call(cb)
    with pytest.raises(CircuitOpen):  # still within cooldown
        _call(cb)
    clock.t = 31.0  # past cooldown: one probe allowed
    assert _call(cb) == {"ok": 1}  # probe succeeds -> closed
    assert _call(cb) == {"ok": 2}  # normal service resumes


def test_failed_probe_reopens():
    clock = Clock()
    inner = ScriptedHttp([ConnectorError("down", status=503)] * 3)
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=2, reset_after=30.0), now=clock
    )
    for _ in range(2):
        with pytest.raises(ConnectorError):
            _call(cb)
    clock.t = 31.0
    with pytest.raises(ConnectorError):  # the probe fails
        _call(cb)
    clock.t = 40.0  # still within a fresh cooldown -> open again, fail fast
    with pytest.raises(CircuitOpen):
        _call(cb)


def test_client_errors_do_not_trip():
    inner = ScriptedHttp([ConnectorError("not found", status=404)] * 5)
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=3), now=Clock()
    )
    for _ in range(5):
        with pytest.raises(ConnectorError):
            _call(cb)
    assert inner.calls == 5  # a 404 is an answer, not an outage: never opened


def test_success_resets_the_failure_count():
    inner = ScriptedHttp(
        [
            ConnectorError("x", status=500),
            {"ok": 1},
            ConnectorError("x", status=500),
            ConnectorError("x", status=500),
        ]
    )
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=3), now=Clock()
    )
    with pytest.raises(ConnectorError):
        _call(cb)
    assert _call(cb) == {"ok": 1}  # success clears the streak
    for _ in range(2):
        with pytest.raises(ConnectorError):
            _call(cb)
    assert inner.calls == 4  # only 2 consecutive after reset: never opened


def test_connection_level_error_trips():
    # status=None means a connection-level failure (no HTTP response) -> outage
    inner = ScriptedHttp([ConnectorError("conn refused")] * 2)
    cb = CircuitBreakerHttpClient(
        inner, policy=BreakerPolicy(failure_threshold=2), now=Clock()
    )
    for _ in range(2):
        with pytest.raises(ConnectorError):
            _call(cb)
    with pytest.raises(CircuitOpen):
        _call(cb)
