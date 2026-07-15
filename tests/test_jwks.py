"""HttpJwks must survive an IdP blip. Signing keys rotate slowly and overlap,
so a slightly stale JWKS still verifies live tokens; a transient fetch failure
must NOT break verification fleet-wide. These pin stale-on-error, the retry
throttle during an outage, and that a cold cache still raises (nothing to
serve)."""

import httpx
import pytest

from hallpass.identity import HttpJwks

DOC = {"keys": [{"kid": "k1", "kty": "RSA"}]}


class _Resp:
    def __init__(self, doc):
        self._doc = doc

    def raise_for_status(self):
        pass

    def json(self):
        return self._doc


def test_serves_stale_document_on_refresh_failure(monkeypatch):
    calls = {"n": 0}

    def ok(url, timeout):
        calls["n"] += 1
        return _Resp(DOC)

    monkeypatch.setattr(httpx, "get", ok)
    clock = {"t": 1000.0}
    jwks = HttpJwks(
        "https://idp.example/jwks", ttl_seconds=300.0, now=lambda: clock["t"]
    )

    assert jwks.get() == DOC and calls["n"] == 1  # cold fetch
    assert jwks.get() == DOC and calls["n"] == 1  # within TTL -> cached, no fetch

    # TTL lapses and the IdP goes down
    clock["t"] += 301.0

    def down(url, timeout):
        calls["n"] += 1
        raise httpx.ConnectError("idp unreachable")

    monkeypatch.setattr(httpx, "get", down)
    assert jwks.get() == DOC  # STALE served, not an exception
    assert calls["n"] == 2  # it did attempt the refresh once
    # a second call inside the error-retry window serves stale WITHOUT re-fetch
    assert jwks.get() == DOC and calls["n"] == 2


def test_retries_after_error_retry_window(monkeypatch):
    clock = {"t": 0.0}
    jwks = HttpJwks(
        "https://idp.example/jwks",
        ttl_seconds=300.0,
        error_retry_seconds=30.0,
        now=lambda: clock["t"],
    )
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(DOC))
    jwks.get()  # prime the cache

    clock["t"] += 301.0  # TTL lapsed
    calls = {"n": 0}

    def down(url, timeout):
        calls["n"] += 1
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "get", down)
    jwks.get()  # attempt 1 (fails, serves stale)
    jwks.get()  # within retry window -> no attempt
    assert calls["n"] == 1
    clock["t"] += 31.0  # error-retry window passed
    jwks.get()  # attempt 2
    assert calls["n"] == 2


def test_cold_cache_still_raises(monkeypatch):
    """A never-fetched source has nothing to serve stale, so a down IdP must
    surface -- the deploy must fail loudly, not silently verify nothing."""

    def down(url, timeout):
        raise httpx.ConnectError("idp unreachable")

    monkeypatch.setattr(httpx, "get", down)
    jwks = HttpJwks("https://idp.example/jwks")
    with pytest.raises(httpx.ConnectError):
        jwks.get()


def test_recovers_to_fresh_after_outage(monkeypatch):
    clock = {"t": 0.0}
    jwks = HttpJwks(
        "https://idp.example/jwks", ttl_seconds=300.0, now=lambda: clock["t"]
    )
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(DOC))
    jwks.get()
    clock["t"] += 301.0
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout: (_ for _ in ()).throw(httpx.ConnectError("x")),
    )
    assert jwks.get() == DOC  # stale during outage
    # IdP recovers with a rotated document
    rotated = {"keys": [{"kid": "k2", "kty": "RSA"}]}
    clock["t"] += 31.0  # past the retry window
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Resp(rotated))
    assert jwks.get() == rotated  # picks up fresh keys once the IdP is back
