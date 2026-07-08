"""The OAuth pending-state store must be single-use and expiring, and the
SQLite-backed one must behave identically to the in-memory one while surviving
across separate store instances (the point: start() and finish() on different
processes behind a load balancer). A durable store that lost single-use or TTL
semantics would reopen CSRF/replay windows the in-memory store closes."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    OAuthConnect,
    OAuthError,
    OAuthProvider,
    SqlitePendingStore,
)
from hallpass.oauth import _Pending


def _rec(created_at=1000.0, scopes=("repo", "read:user")):
    return _Pending(
        subject="alice",
        service="github",
        code_verifier="v",
        created_at=created_at,
        scopes=scopes,
    )


def test_put_then_pop_returns_the_record():
    store = SqlitePendingStore(now=lambda: 1000.0)
    store.put("s1", _rec())
    got = store.pop("s1")
    assert got is not None
    assert got.subject == "alice" and got.service == "github"
    assert got.scopes == ("repo", "read:user")  # round-trips through the table
    store.close()


def test_state_is_single_use():
    store = SqlitePendingStore(now=lambda: 1000.0)
    store.put("s1", _rec())
    assert store.pop("s1") is not None
    assert store.pop("s1") is None  # gone after first pop
    store.close()


def test_expired_state_is_rejected_and_consumed():
    clock = {"t": 1000.0}
    store = SqlitePendingStore(ttl_seconds=600.0, now=lambda: clock["t"])
    store.put("s1", _rec(created_at=1000.0))
    clock["t"] = 2000.0  # past TTL
    assert store.pop("s1") is None
    # even expired, it was removed (single-use), so a later in-window pop is None
    clock["t"] = 1000.0
    assert store.pop("s1") is None
    store.close()


def test_unknown_state_returns_none():
    store = SqlitePendingStore()
    assert store.pop("never") is None
    store.close()


def test_survives_across_store_instances(tmp_path):
    path = str(tmp_path / "pending.sqlite3")
    s1 = SqlitePendingStore(path=path, now=lambda: 1000.0)
    s1.put("s1", _rec())
    s1.close()
    # a different instance (another process/replica) finishes the flow
    s2 = SqlitePendingStore(path=path, now=lambda: 1000.0)
    got = s2.pop("s1")
    assert got is not None and got.subject == "alice"
    s2.close()


def test_end_to_end_connect_with_durable_store():
    class FakeTokenHttp:
        def post_form(self, url, *, data, headers):
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}

    vault = CredentialVault(Fernet.generate_key())
    provider = OAuthProvider(
        authorize_url="https://p/auth",
        token_url="https://p/token",
        client_id="c",
        redirect_uri="https://a/cb",
        client_secret="s",
        scopes=("repo",),
    )
    connect = OAuthConnect(
        vault=vault,
        providers={"github": provider},
        token_http=FakeTokenHttp(),
        pending=SqlitePendingStore(),
    )
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    assert connect.finish(state, "code") == "alice"
    assert vault.fetch("alice", "github") == "at-1"
    with pytest.raises(OAuthError):
        connect.finish(state, "code")  # single-use enforced end to end
    vault.close()
