"""Declarative REST connectors, tested against a fake transport so nothing
touches the network. The checks: the request is built correctly (method,
url, path substitution, query, body), the vaulted credential is attached in
the service's auth style, a missing credential fails clearly, and the whole
thing still flows through hallpass gating."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    ConnectorError,
    CredentialVault,
    Endpoint,
    Hallpass,
    RestConnector,
    RestService,
    StaticJwks,
    TokenVerifier,
    dev_app,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


class FakeHttp:
    """Records the last request and returns canned data; never networks."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self._result = result if result is not None else {"ok": True}

    def request(self, method, url, *, headers, params, json):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        return self._result


GH = RestService(
    service="demo",
    base_url="https://api.example.com",
    auth="bearer",
    headers={"Accept": "application/json"},
    endpoints=(
        Endpoint(
            name="get_item",
            description="Get an item.",
            method="GET",
            path="/orgs/{org}/items/{item_id}",
            scopes=frozenset({"demo:read"}),
            query=("expand",),
        ),
        Endpoint(
            name="create_item",
            description="Create an item.",
            method="POST",
            path="/orgs/{org}/items",
            scopes=frozenset({"demo:write"}),
            body=("title", "note"),
            required=frozenset({"title"}),
        ),
    ),
)


@pytest.fixture()
def app(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault)
    yield application, vault
    vault.close()


def test_get_builds_request_and_attaches_bearer(app, keypair):
    application, vault = app
    http = FakeHttp(result={"id": "42"})
    application.add_connector(RestConnector(GH, http=http))
    vault.store("alice", "demo", "tok-abc")  # the user's connected credential

    token = mint(keypair, sub="alice", scope="demo:read")
    result = application.call_tool(
        token, "get_item", {"org": "acme", "item_id": "42", "expand": "full"}
    )
    assert result == {"id": "42"}
    call = http.calls[-1]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.example.com/orgs/acme/items/42"
    assert call["params"] == {"expand": "full"}
    assert call["json"] is None
    assert call["headers"]["Authorization"] == "Bearer tok-abc"
    assert call["headers"]["Accept"] == "application/json"


def test_post_sends_body(app, keypair):
    application, vault = app
    http = FakeHttp()
    application.add_connector(RestConnector(GH, http=http))
    vault.store("alice", "demo", "tok-abc")
    token = mint(keypair, sub="alice", scope="demo:write")
    application.call_tool(
        token, "create_item", {"org": "acme", "title": "Hi", "note": "n"}
    )
    call = http.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/orgs/acme/items"
    assert call["json"] == {"title": "Hi", "note": "n"}


def test_missing_credential_raises_connector_error(app, keypair):
    application, vault = app
    application.add_connector(RestConnector(GH, http=FakeHttp()))
    token = mint(keypair, sub="stranger", scope="demo:read")  # never connected
    with pytest.raises(ConnectorError):
        application.call_tool(token, "get_item", {"org": "a", "item_id": "1"})


def test_gating_applies_to_rest_tools(app, keypair):
    application, vault = app
    application.add_connector(RestConnector(GH, http=FakeHttp()))
    vault.store("bob", "demo", "tok")
    # bob has read but not write: create_item must be absent and uncallable
    token = mint(keypair, sub="bob", scope="demo:read")
    names = {t.name for t in application.list_tools(token)}
    assert names == {"get_item"}
    with pytest.raises(Exception):
        application.call_tool(token, "create_item", {"org": "a", "title": "x"})


def test_input_schema_marks_path_and_required():
    ep = GH.endpoints[1]  # create_item
    schema = ep.input_schema()
    assert set(schema["properties"]) == {"org", "title", "note"}
    assert set(schema["required"]) == {"org", "title"}  # path param + declared required


def test_auth_styles():
    from hallpass.rest import _apply_auth

    def svc(auth):
        return RestService(service="s", base_url="x", endpoints=(), auth=auth)

    assert _apply_auth(svc("bearer"), "c") == ({"Authorization": "Bearer c"}, {})
    assert _apply_auth(svc("token"), "c") == ({"Authorization": "token c"}, {})
    assert _apply_auth(svc("bot"), "c") == ({"Authorization": "Bot c"}, {})
    assert _apply_auth(svc("basic"), "c") == ({"Authorization": "Basic c"}, {})
    assert _apply_auth(svc(("header", "X-Api-Key")), "c") == ({"X-Api-Key": "c"}, {})
    assert _apply_auth(svc(("query", "api_token")), "c") == ({}, {"api_token": "c"})
    # templated auth for non-standard schemes (PagerDuty: "Token token=<key>")
    assert _apply_auth(svc(("template", "Token token={cred}")), "c") == (
        {"Authorization": "Token token=c"},
        {},
    )


def test_pagerduty_uses_templated_auth_end_to_end(app, keypair):
    from hallpass import catalog

    application, vault = app
    http = FakeHttp()
    application.add_connector(catalog.load("pagerduty", http=http))
    vault.store("alice", "pagerduty", "pdkey")  # the user's PagerDuty API key

    token = mint(keypair, sub="alice", scope="pagerduty:read")
    application.call_tool(token, "pagerduty_list_services", {})
    # the templated scheme renders the key into a non-Bearer Authorization header
    assert http.calls[-1]["headers"]["Authorization"] == "Token token=pdkey"


def test_catalog_is_well_formed():
    from hallpass import catalog

    all_names = catalog.names()
    assert len(all_names) >= 30  # a broad catalog
    seen_tools: set[str] = set()
    for name in all_names:
        # per-tenant services need a base URL supplied at load
        kwargs = {"http": FakeHttp()}
        if catalog.requires_base_url(name):
            kwargs["base_url"] = "https://tenant.example.com"
        specs = catalog.load(name, **kwargs).tools()
        assert specs, f"{name} has no tools"
        for spec in specs:
            assert spec.name not in seen_tools, f"duplicate tool name {spec.name}"
            seen_tools.add(spec.name)
            assert spec.input_schema is not None
    assert len(seen_tools) >= 80  # tens of tools across the catalog


def test_per_tenant_service_requires_base_url():
    from hallpass import catalog

    with pytest.raises(ValueError):
        catalog.load("jira", http=FakeHttp())  # no base_url
    conn = catalog.load("jira", http=FakeHttp(), base_url="https://site.atlassian.net")
    assert conn.tools()


def test_load_all_skips_per_tenant():
    from hallpass import catalog

    services = {c.service for c in catalog.load_all(http=FakeHttp())}
    assert "github" in services
    assert "jira" not in services  # per-tenant, skipped by load_all


def test_catalog_connector_runs_end_to_end(keypair):
    from hallpass import catalog

    http = FakeHttp(result=[{"full_name": "a/b"}])
    gh = catalog.load("github", http=http)
    app, token = dev_app(connectors=[gh])
    # connect the user's github credential (via the vault the dev app built),
    # then call a catalog tool
    app._vault.store("alice", "github", "ghp_xxx")
    out = app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert out == [{"full_name": "a/b"}]
    assert http.calls[-1]["headers"]["Authorization"] == "Bearer ghp_xxx"
    assert http.calls[-1]["url"] == "https://api.github.com/user/repos"
    app.close()


def test_unknown_catalog_name_raises():
    from hallpass import catalog

    with pytest.raises(KeyError):
        catalog.load("does-not-exist")
