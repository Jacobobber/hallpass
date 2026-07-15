"""Readiness is a real dependency probe, not a constant. A replica whose shared
database or cache is unreachable must report NOT ready so a load balancer drains
it, and the probe must never write (no counter, no result) or leak a host/secret.
These use fakes so the behavior is asserted without a real Postgres or Redis."""

import pytest

from hallpass import StaticJwks, build

EMPTY_JWKS = StaticJwks({"keys": []})


def _app(**kwargs):
    # jwks is unused by readiness (it only matters on verify), so an empty one
    # lets us build an app cheaply and probe its backends.
    return build(issuer="i", audience="a", jwks=EMPTY_JWKS, **kwargs)


def test_ready_on_default_sqlite_vault():
    app = _app()
    ready, checks = app.check_readiness()
    assert ready is True
    assert checks == {"vault": "ok"}  # no idempotency wired -> not probed
    app.close()


def test_not_ready_when_vault_backend_unreachable():
    app = _app()
    app.close()  # closes the vault's SQLite connection
    ready, checks = app.check_readiness()
    assert ready is False
    assert checks["vault"] == "error"


class _FakeIdem:
    """Records reads; a real idempotency store's get() is a networked round-trip,
    which is exactly what readiness wants to exercise."""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.gets: list[tuple[str, str, str]] = []
        self.puts: list[tuple[str, str, str]] = []

    def get(self, subject: str, tool: str, key: str):
        if self.boom:
            raise RuntimeError("cache unreachable")
        self.gets.append((subject, tool, key))
        return False, None

    def put(self, subject: str, tool: str, key: str, result) -> None:
        self.puts.append((subject, tool, key))


def test_ready_probes_idempotency_read_only():
    idem = _FakeIdem()
    app = _app(idempotency=idem)
    ready, checks = app.check_readiness()
    assert ready is True
    assert checks == {"vault": "ok", "idempotency": "ok"}
    # the probe read once and never wrote (no counter/result side effect)
    assert len(idem.gets) == 1 and idem.puts == []
    app.close()


def test_not_ready_when_idempotency_errors():
    app = _app(idempotency=_FakeIdem(boom=True))
    ready, checks = app.check_readiness()
    assert ready is False
    assert checks["vault"] == "ok" and checks["idempotency"] == "error"
    app.close()


def test_readiness_never_leaks_more_than_status():
    """The only values are the fixed component names and ok/error -- nothing
    that could carry a DSN, host, or secret."""
    app = _app(idempotency=_FakeIdem())
    _, checks = app.check_readiness()
    assert set(checks) <= {"vault", "idempotency"}
    assert set(checks.values()) <= {"ok", "error"}
    app.close()


class _CountingVault:
    """A minimal vault stand-in that counts backend round-trips, to prove the
    readiness cache actually spares the backend."""

    durable = True

    def __init__(self) -> None:
        self.calls = 0

    def services(self, subject: str):
        self.calls += 1
        return []

    def close(self) -> None:
        pass


def _app_with_vault(vault, **kwargs):
    from hallpass import Hallpass, TokenVerifier

    verifier = TokenVerifier(issuer="i", audience="a", jwks=EMPTY_JWKS)
    return Hallpass(verifier=verifier, vault=vault, **kwargs)


def test_readiness_is_cached_within_ttl():
    vault = _CountingVault()
    app = _app_with_vault(vault, readiness_ttl=5.0)
    app.check_readiness()
    app.check_readiness()
    assert vault.calls == 1  # second call served from cache, no backend hit


def test_readiness_ttl_zero_disables_cache():
    vault = _CountingVault()
    app = _app_with_vault(vault, readiness_ttl=0.0)
    app.check_readiness()
    app.check_readiness()
    assert vault.calls == 2  # ttl<=0 -> always a fresh probe


def test_build_redis_url_wires_shared_rate_limit():
    """build(redis_url=..., rate_limit=...) selects the shared Redis limiter.
    Construction is lazy (no connection), so this proves wiring; connectivity is
    the Postgres/Redis integration job's concern. Skips without the redis extra."""
    pytest.importorskip("redis")
    app = build(
        issuer="i",
        audience="a",
        jwks=EMPTY_JWKS,
        redis_url="redis://localhost:6379/0",
        rate_limit=(10, 60.0),
    )
    assert app.has_rate_limiter is True
    app.close()
