"""Seamless OAuth: a stale access token should not surface to the user. When
an operator wires OAuthConnect.attach_refresh into a connector, a 401/403 from
the service renews the token and retries the call once. valid_token() covers
the proactive path: refresh before the call if the stored token is expiring.

The failures these prevent: a user's connector silently breaking the moment
their access token expires, and a refresh firing on errors it can't fix (500s)
or looping when the refresh changes nothing."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    ConnectorError,
    CredentialVault,
    OAuthConnect,
    OAuthError,
    OAuthProvider,
    catalog,
    dev_app,
)


class FakeTokenHttp:
    """Token endpoint: the code exchange yields at-123, a refresh yields
    at-NEW, so a test can tell a refreshed token apart from the first one."""

    def __init__(self):
        self.calls = []

    def post_form(self, url, *, data, headers):
        self.calls.append(data)
        if data["grant_type"] == "refresh_token":
            return {"access_token": "at-NEW", "expires_in": 3600}
        return {"access_token": "at-123", "refresh_token": "rt-456", "expires_in": 3600}


def _provider(**over):
    base = dict(
        authorize_url="https://prov.example/authorize",
        token_url="https://prov.example/token",
        client_id="cid",
        redirect_uri="https://app.example/callback",
        client_secret="csecret",
        scopes=("repo",),
    )
    base.update(over)
    return OAuthProvider(**base)


def _oauth(vault, http):
    return OAuthConnect(vault=vault, providers={"github": _provider()}, token_http=http)


def _connect(connect):
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")


class SwitchingRestHttp:
    """Rejects any token other than ``good`` with a 401, accepts ``good`` with
    a 200. Records the Authorization header of every attempt."""

    def __init__(self, good):
        self._good = good
        self.auth_seen = []

    def request(self, method, url, *, headers, params, json):
        self.auth_seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == f"Bearer {self._good}":
            return {"ok": True}
        raise ConnectorError("unauthorized", status=401)


def test_stale_token_auto_refreshes_and_retries():
    rest_http = SwitchingRestHttp(good="at-NEW")
    gh = catalog.load("github", http=rest_http)
    app, token = dev_app(connectors=[gh])
    connect = OAuthConnect(
        vault=app._vault,
        providers={
            "github": catalog.oauth_provider(
                "github", client_id="c", redirect_uri="https://a/cb", client_secret="s"
            )
        },
        token_http=FakeTokenHttp(),
    )
    connect.attach_refresh(gh)
    _connect(connect)  # stores at-123

    out = app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert out == {"ok": True}
    # first attempt used the stale token, the retry used the refreshed one
    assert rest_http.auth_seen == ["Bearer at-123", "Bearer at-NEW"]
    app.close()


def test_non_auth_error_is_not_retried():
    class Failing:
        def __init__(self):
            self.n = 0

        def request(self, method, url, *, headers, params, json):
            self.n += 1
            raise ConnectorError("server blew up", status=500)

    rest_http = Failing()
    gh = catalog.load("github", http=rest_http)
    app, token = dev_app(connectors=[gh])
    token_http = FakeTokenHttp()
    connect = OAuthConnect(
        vault=app._vault,
        providers={
            "github": catalog.oauth_provider(
                "github", client_id="c", redirect_uri="https://a/cb", client_secret="s"
            )
        },
        token_http=token_http,
    )
    connect.attach_refresh(gh)
    _connect(connect)

    with pytest.raises(ConnectorError):
        app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert rest_http.n == 1  # not retried
    assert not any(c["grant_type"] == "refresh_token" for c in token_http.calls)
    app.close()


def test_401_without_refresher_propagates():
    rest_http = SwitchingRestHttp(good="never")  # every attempt 401s
    gh = catalog.load("github", http=rest_http)  # no attach_refresh
    app, token = dev_app(connectors=[gh])
    connect = OAuthConnect(
        vault=app._vault,
        providers={
            "github": catalog.oauth_provider(
                "github", client_id="c", redirect_uri="https://a/cb", client_secret="s"
            )
        },
        token_http=FakeTokenHttp(),
    )
    _connect(connect)

    with pytest.raises(ConnectorError):
        app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert rest_http.auth_seen == ["Bearer at-123"]  # tried once, no retry
    app.close()


def test_valid_token_refreshes_when_expired():
    clock = {"t": 1000.0}
    vault = CredentialVault(Fernet.generate_key())
    http = FakeTokenHttp()
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider()},
        token_http=http,
        now=lambda: clock["t"],
    )
    _connect(connect)  # expires_at = 1000 + 3600 = 4600
    clock["t"] = 5000.0  # past expiry

    assert connect.valid_token("alice", "github") == "at-NEW"
    assert vault.fetch("alice", "github") == "at-NEW"
    vault.close()


def test_valid_token_returns_current_when_fresh():
    clock = {"t": 1000.0}
    vault = CredentialVault(Fernet.generate_key())
    http = FakeTokenHttp()
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider()},
        token_http=http,
        now=lambda: clock["t"],
    )
    _connect(connect)

    assert connect.valid_token("alice", "github") == "at-123"  # nowhere near expiry
    assert not any(c["grant_type"] == "refresh_token" for c in http.calls)
    vault.close()


def test_valid_token_unconnected_errors():
    vault = CredentialVault(Fernet.generate_key())
    connect = _oauth(vault, FakeTokenHttp())
    with pytest.raises(OAuthError):
        connect.valid_token("nobody", "github")
    vault.close()
