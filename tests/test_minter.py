"""Minter-as-service: each agent gets its OWN service token from the IdP's
client-credentials grant, not a shared or human identity. Each test names the
property it pins. The token HTTP is faked, so no network and no real IdP."""

import pytest

from hallpass import (
    AgentClient,
    AgentSpec,
    ClientCredentialsMinter,
    OAuthError,
    Team,
)


class _FakeTokenHttp:
    """Records the form posted to the token endpoint and returns a canned
    token response (or a caller-supplied one)."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or {"access_token": "minted-token"}

    def post_form(self, url, *, data, headers):
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        return self._response


def _minter(http, *, audience=None):
    return ClientCredentialsMinter(
        token_url="https://idp.example/oauth/token",
        clients={
            "reviewer": AgentClient("reviewer-cid", "reviewer-secret"),
            "messenger": AgentClient("messenger-cid", "messenger-secret"),
        },
        audience=audience,
        http=http,
    )


def test_mints_with_agents_own_client_credentials():
    http = _FakeTokenHttp()
    minter = _minter(http)
    token = minter("reviewer", frozenset({"github:read"}))
    assert token == "minted-token"
    (call,) = http.calls
    assert call["url"] == "https://idp.example/oauth/token"
    assert call["data"]["grant_type"] == "client_credentials"
    assert call["data"]["client_id"] == "reviewer-cid"
    assert call["data"]["client_secret"] == "reviewer-secret"
    assert call["data"]["scope"] == "github:read"


def test_each_agent_uses_its_own_client():
    http = _FakeTokenHttp()
    minter = _minter(http)
    minter("reviewer", frozenset())
    minter("messenger", frozenset())
    ids = [c["data"]["client_id"] for c in http.calls]
    assert ids == ["reviewer-cid", "messenger-cid"]  # distinct identities, no sharing


def test_unregistered_agent_is_refused_no_fallback():
    """An agent with no client-credentials identity is refused -- there is no
    silent fallback to a shared identity."""
    http = _FakeTokenHttp()
    minter = _minter(http)
    with pytest.raises(OAuthError, match="no client-credentials identity"):
        minter("stranger", frozenset())
    assert http.calls == []  # never hit the token endpoint


def test_missing_access_token_raises():
    http = _FakeTokenHttp(response={"error": "invalid_client"})
    minter = _minter(http)
    with pytest.raises(OAuthError, match="no access_token"):
        minter("reviewer", frozenset())


def test_audience_included_when_set():
    http = _FakeTokenHttp()
    minter = _minter(http, audience="https://hallpass.example/api")
    minter("reviewer", frozenset())
    assert http.calls[0]["data"]["audience"] == "https://hallpass.example/api"


def test_register_adds_an_agent():
    http = _FakeTokenHttp()
    minter = ClientCredentialsMinter(token_url="https://idp.example/token", http=http)
    minter.register("late", AgentClient("late-cid", "late-secret"))
    minter("late", frozenset({"x:y"}))
    assert http.calls[0]["data"]["client_id"] == "late-cid"


def test_drops_into_team_as_the_mint_callable():
    """ClientCredentialsMinter is a callable AgentMinter, so it is exactly what
    Team(mint=...) wants."""
    http = _FakeTokenHttp()
    minter = _minter(http)

    class _Handle:
        def __init__(self, name):
            self.name = name

        def alive(self):
            return True

        def terminate(self):
            pass

    class _Spawner:
        def __init__(self):
            self.launched = []

        def spawn(self, spec, env):
            self.launched.append((spec.name, env["HALLPASS_AGENT_TOKEN"]))
            return _Handle(spec.name)

    spawner = _Spawner()
    team = Team(mint=minter, spawner=spawner, channel="work")
    team.spawn(AgentSpec("reviewer", scopes=frozenset({"github:read"})))
    assert spawner.launched == [("reviewer", "minted-token")]
