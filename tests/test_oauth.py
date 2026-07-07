"""The OAuth connect flow puts a user's service token in the vault so the
catalog's connectors work end to end. The properties that matter: state is
single-use CSRF protection, PKCE is carried correctly, the code exchange
stores the token where the connector reads it, refresh renews it, and no
token/code/secret ever appears in an error."""

import json

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    OAuthConnect,
    OAuthError,
    OAuthProvider,
    catalog,
    dev_app,
)


class FakeTokenHttp:
    """Records the last token request and returns canned tokens."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or {
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
            "scope": "repo",
        }

    def post_form(self, url, *, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return dict(self._response)


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


@pytest.fixture()
def setup():
    vault = CredentialVault(Fernet.generate_key())
    http = FakeTokenHttp()
    connect = OAuthConnect(
        vault=vault, providers={"github": _provider()}, token_http=http
    )
    yield connect, vault, http
    vault.close()


def test_start_builds_authorize_url_with_state_and_pkce(setup):
    connect, _, _ = setup
    url = connect.start("alice", "github")
    assert url.startswith("https://prov.example/authorize?")
    assert "client_id=cid" in url
    assert "state=" in url
    assert "code_challenge=" in url and "code_challenge_method=S256" in url
    assert "scope=repo" in url


def test_finish_exchanges_code_and_stores_token(setup):
    connect, vault, http = setup
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    subject = connect.finish(state, "the-auth-code")
    assert subject == "alice"
    # the connector reads the raw access token from the service slot
    assert vault.fetch("alice", "github") == "at-123"
    # the exchange sent the code, the redirect, and the pkce verifier
    sent = http.calls[-1]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-auth-code"
    assert "code_verifier" in sent


def test_state_is_single_use(setup):
    connect, _, _ = setup
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code-1")
    with pytest.raises(OAuthError):
        connect.finish(state, "code-2")  # same state cannot be replayed


def test_unknown_state_rejected(setup):
    connect, _, _ = setup
    with pytest.raises(OAuthError):
        connect.finish("never-issued", "code")


def test_expired_state_rejected():
    clock = {"t": 1000.0}
    vault = CredentialVault(Fernet.generate_key())
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider()},
        token_http=FakeTokenHttp(),
        now=lambda: clock["t"],
    )
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    clock["t"] += 10_000  # well past the pending TTL
    with pytest.raises(OAuthError):
        connect.finish(state, "code")
    vault.close()


def test_refresh_renews_access_token(setup):
    connect, vault, http = setup
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")
    http._response = {"access_token": "at-NEW", "expires_in": 3600}  # no new refresh
    new = connect.refresh("alice", "github")
    assert new == "at-NEW"
    assert vault.fetch("alice", "github") == "at-NEW"
    # the old refresh token is preserved when the response omits one
    bundle = json.loads(vault.fetch("alice", "github:oauth"))
    assert bundle["refresh_token"] == "rt-456"
    assert http.calls[-1]["data"]["grant_type"] == "refresh_token"


def test_refresh_without_stored_token_errors(setup):
    connect, _, _ = setup
    with pytest.raises(OAuthError):
        connect.refresh("nobody", "github")


def test_public_client_omits_secret_uses_pkce():
    vault = CredentialVault(Fernet.generate_key())
    http = FakeTokenHttp()
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider(client_secret=None, use_pkce=True)},
        token_http=http,
    )
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")
    assert "client_secret" not in http.calls[-1]["data"]
    assert "code_verifier" in http.calls[-1]["data"]
    vault.close()


def test_errors_never_leak_secrets():
    vault = CredentialVault(Fernet.generate_key())
    # a token endpoint that returns no access_token
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider()},
        token_http=FakeTokenHttp(response={"error": "bad"}),
    )
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    try:
        connect.finish(state, "super-secret-code")
    except OAuthError as e:
        assert "super-secret-code" not in str(e)
    vault.close()


def test_unconfigured_provider_errors(setup):
    connect, _, _ = setup
    with pytest.raises(OAuthError):
        connect.start("alice", "notion")  # not in providers


# -- catalog integration --------------------------------------------------


def test_catalog_oauth_provider_builds_known_service():
    provider = catalog.oauth_provider(
        "github",
        client_id="cid",
        redirect_uri="https://app.example/cb",
        client_secret="sec",
    )
    assert provider.authorize_url.startswith("https://github.com/login/oauth/authorize")
    assert provider.token_url.endswith("access_token")
    assert provider.scopes  # defaulted from the registry


def test_catalog_oauth_services_listed():
    services = catalog.oauth_services()
    assert "github" in services and "slack" in services
    assert len(services) >= 15


def test_unknown_oauth_service_errors():
    with pytest.raises(KeyError):
        catalog.oauth_provider("nope", client_id="x", redirect_uri="y")


def test_end_to_end_connect_then_call_catalog_tool():
    """The whole point: connect a user via OAuth, then the catalog connector
    uses the stored token to make a call."""

    class FakeRestHttp:
        def request(self, method, url, *, headers, params, json):
            self.last = {"url": url, "headers": headers}
            return [{"full_name": "a/b"}]

    rest_http = FakeRestHttp()
    gh = catalog.load("github", http=rest_http)
    app, token = dev_app(connectors=[gh])

    # OAuth connect writes the token into the same vault the app uses
    connect = OAuthConnect(
        vault=app._vault,
        providers={
            "github": catalog.oauth_provider(
                "github",
                client_id="cid",
                redirect_uri="https://app/cb",
                client_secret="s",
            )
        },
        token_http=FakeTokenHttp(),
    )
    url = connect.start("alice", "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")

    out = app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert out == [{"full_name": "a/b"}]
    assert rest_http.last["headers"]["Authorization"] == "Bearer at-123"
    app.close()
