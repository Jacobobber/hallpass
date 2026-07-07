"""Connecting a service should leave a record the user can see and revoke.
The properties that matter: finish() records the granted scopes, disconnect()
forgets BOTH the access token and the refresh bundle (a half-revoke that left
the refresh token behind would be a real leak) and drops the consent, and the
provider's echoed scope wins over what was merely requested."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    InMemoryConsentLedger,
    OAuthConnect,
    OAuthProvider,
)


class FakeTokenHttp:
    def __init__(self, response=None):
        self._response = response or {
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
            "scope": "repo read:user",
        }

    def post_form(self, url, *, data, headers):
        return dict(self._response)


def _provider(**over):
    base = dict(
        authorize_url="https://prov.example/authorize",
        token_url="https://prov.example/token",
        client_id="cid",
        redirect_uri="https://app.example/cb",
        client_secret="csecret",
        scopes=("repo",),
    )
    base.update(over)
    return OAuthProvider(**base)


def _make(http=None):
    vault = CredentialVault(Fernet.generate_key())
    ledger = InMemoryConsentLedger()
    connect = OAuthConnect(
        vault=vault,
        providers={"github": _provider()},
        token_http=http or FakeTokenHttp(),
        consent=ledger,
    )
    return connect, vault, ledger


def _connect(connect, subject="alice"):
    url = connect.start(subject, "github")
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")


def test_finish_records_consent_with_granted_scope():
    connect, vault, ledger = _make()
    _connect(connect)
    consents = connect.consents("alice")
    assert len(consents) == 1
    record = consents[0]
    assert record.service == "github"
    # the provider echoed "repo read:user"; that wins over the requested "repo"
    assert record.scopes == ("repo", "read:user")
    assert record.granted_at is not None
    vault.close()


def test_requested_scope_used_when_provider_echoes_none():
    connect, vault, ledger = _make(
        http=FakeTokenHttp(response={"access_token": "at", "expires_in": 3600})
    )
    url = connect.start("alice", "github", scopes=["a", "b"])
    state = url.split("state=")[1].split("&")[0]
    connect.finish(state, "code")
    assert connect.consents("alice")[0].scopes == ("a", "b")
    vault.close()


def test_disconnect_clears_token_bundle_and_consent():
    connect, vault, ledger = _make()
    _connect(connect)
    assert vault.fetch("alice", "github") == "at-123"
    assert vault.fetch("alice", "github:oauth") is not None

    assert connect.disconnect("alice", "github") is True
    # both the access token and the refresh bundle are gone
    assert vault.fetch("alice", "github") is None
    assert vault.fetch("alice", "github:oauth") is None
    # and so is the consent record
    assert connect.consents("alice") == []
    vault.close()


def test_disconnect_returns_false_when_nothing_connected():
    connect, vault, ledger = _make()
    assert connect.disconnect("nobody", "github") is False
    vault.close()


def test_consents_lists_only_the_callers_services():
    connect, vault, ledger = _make()
    _connect(connect, "alice")
    _connect(connect, "bob")
    assert [c.subject for c in connect.consents("alice")] == ["alice"]
    assert [c.subject for c in connect.consents("bob")] == ["bob"]
    vault.close()


def test_ledger_is_optional():
    # Without a ledger, connect still works and consents() is simply empty.
    vault = CredentialVault(Fernet.generate_key())
    connect = OAuthConnect(
        vault=vault, providers={"github": _provider()}, token_http=FakeTokenHttp()
    )
    _connect(connect)
    assert connect.consents("alice") == []
    # disconnect still clears the vault even with no ledger
    assert connect.disconnect("alice", "github") is True
    vault.close()


def test_in_memory_ledger_grant_get_revoke():
    ledger = InMemoryConsentLedger()
    ledger.grant("alice", "svc", ["x", "y"], at=1000.0)
    got = ledger.get("alice", "svc")
    assert got is not None and got.scopes == ("x", "y") and got.granted_at == 1000.0
    assert ledger.revoke("alice", "svc") is True
    assert ledger.get("alice", "svc") is None
    assert ledger.revoke("alice", "svc") is False


def test_grant_replaces_prior_record():
    ledger = InMemoryConsentLedger()
    ledger.grant("alice", "svc", ["x"], at=1.0)
    ledger.grant("alice", "svc", ["x", "y"], at=2.0)
    assert len(ledger.list("alice")) == 1
    assert ledger.get("alice", "svc").scopes == ("x", "y")


@pytest.mark.parametrize("subject", ["alice", "bob"])
def test_get_missing_returns_none(subject):
    assert InMemoryConsentLedger().get(subject, "svc") is None
