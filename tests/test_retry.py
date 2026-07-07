"""Transient failures (429, 5xx) should retry with backoff instead of failing
the user's call on the first blip; a Retry-After hint should be obeyed exactly.
And the retry must NOT swallow the failures it can't fix -- a 404 or a 401 is
not retried (401/403 is the connector auto-refresh's job, not a blind retry).

The clock is injected so these prove the schedule without waiting on it."""

import pytest

from hallpass import ConnectorError, RetryingHttpClient, RetryPolicy


class ScriptedHttp:
    """Returns/raises a scripted sequence of outcomes, one per request. An
    outcome is either a ConnectorError (raised) or a value (returned)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, *, headers, params, json):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


def test_retries_transient_then_succeeds():
    inner = ScriptedHttp(
        [
            ConnectorError("boom", status=500),
            ConnectorError("boom", status=503),
            {"ok": 1},
        ]
    )
    clock = FakeClock()
    client = RetryingHttpClient(
        inner, policy=RetryPolicy(max_retries=2, base_delay=1.0), sleep=clock.sleep
    )
    out = client.request("GET", "u", headers={}, params={}, json=None)
    assert out == {"ok": 1}
    assert inner.calls == 3  # two failures + the success
    assert clock.slept == [1.0, 2.0]  # exponential backoff


def test_gives_up_after_max_retries():
    inner = ScriptedHttp([ConnectorError("down", status=503)] * 5)
    clock = FakeClock()
    client = RetryingHttpClient(
        inner, policy=RetryPolicy(max_retries=2), sleep=clock.sleep
    )
    with pytest.raises(ConnectorError):
        client.request("GET", "u", headers={}, params={}, json=None)
    assert inner.calls == 3  # initial + 2 retries, then re-raised
    assert len(clock.slept) == 2


def test_non_retryable_status_is_not_retried():
    inner = ScriptedHttp([ConnectorError("missing", status=404)])
    clock = FakeClock()
    client = RetryingHttpClient(inner, sleep=clock.sleep)
    with pytest.raises(ConnectorError):
        client.request("GET", "u", headers={}, params={}, json=None)
    assert inner.calls == 1
    assert clock.slept == []


def test_auth_failure_is_left_to_the_connector_not_retried():
    # 401/403 must NOT be retried here; auto-refresh owns that path.
    inner = ScriptedHttp([ConnectorError("unauth", status=401)])
    clock = FakeClock()
    client = RetryingHttpClient(inner, sleep=clock.sleep)
    with pytest.raises(ConnectorError):
        client.request("GET", "u", headers={}, params={}, json=None)
    assert inner.calls == 1
    assert clock.slept == []


def test_honors_retry_after_over_backoff():
    inner = ScriptedHttp(
        [ConnectorError("slow down", status=429, retry_after=7.0), {"ok": 1}]
    )
    clock = FakeClock()
    client = RetryingHttpClient(
        inner, policy=RetryPolicy(base_delay=1.0), sleep=clock.sleep
    )
    assert client.request("GET", "u", headers={}, params={}, json=None) == {"ok": 1}
    assert clock.slept == [7.0]  # the header wins over the computed backoff


def test_backoff_is_capped_at_max_delay():
    inner = ScriptedHttp([ConnectorError("x", status=500)] * 4 + [{"ok": 1}])
    clock = FakeClock()
    client = RetryingHttpClient(
        inner,
        policy=RetryPolicy(max_retries=4, base_delay=1.0, max_delay=3.0),
        sleep=clock.sleep,
    )
    assert client.request("GET", "u", headers={}, params={}, json=None) == {"ok": 1}
    assert clock.slept == [1.0, 2.0, 3.0, 3.0]  # 4 then 8 clamped to the cap
