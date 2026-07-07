"""Tool search ranks the tools relevant to a query. The property that
matters most, and the one this suite leans on: search runs AFTER gating,
so it can never surface a tool the caller is not authorized for, no matter
how well the query matches it. Ranking quality is secondary to that."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    Hallpass,
    LexicalRanker,
    StaticJwks,
    TokenVerifier,
    ToolSpec,
    tokenize,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def _spec(name, description, *scopes):
    return ToolSpec(
        name=name,
        description=description,
        required_scopes=frozenset(scopes),
        handler=lambda ctx, **kw: name,
    )


class Tools:
    service = "svc"

    def tools(self):
        return [
            _spec("read_note", "Read the caller's note", "notes:read"),
            _spec("send_email", "Send an email message to a recipient", "mail:send"),
            _spec("list_files", "List files in a folder", "files:read"),
            _spec("delete_everything", "Permanently delete all data", "admin:destroy"),
        ]


@pytest.fixture()
def app(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault)
    application.add_connector(Tools())
    yield application
    vault.close()


# -- the security property ------------------------------------------------


def test_search_never_surfaces_an_ungranted_tool(app, keypair):
    """A caller lacking admin:destroy must not be able to FIND
    delete_everything, even with a query that matches it perfectly."""
    token = mint(keypair, sub="alice", scope="notes:read")
    hits = app.search_tools(token, "permanently delete all data everything")
    names = {t.name for t in hits}
    assert "delete_everything" not in names
    # and a tool she IS granted, matched by the same style of query, is found
    assert "read_note" in {t.name for t in app.search_tools(token, "read my note")}


def test_search_scope_expands_results(app, keypair):
    """With the admin scope, the same query now finds the tool."""
    admin = mint(keypair, sub="root", scope="admin:destroy")
    assert "delete_everything" in {
        t.name for t in app.search_tools(admin, "delete all data")
    }


def test_unauthenticated_search_refused(app):
    with pytest.raises(Exception):
        app.search_tools("garbage", "anything")


def test_misbehaving_ranker_cannot_widen_results(keypair):
    """The invariant is the core's, not the ranker's: even a ranker that
    appends a tool outside the authorized set it was given, the core
    re-filters by name so the ungranted tool never reaches the caller."""

    class MisbehavingRanker:
        def rank(self, query, tools):
            forged = _spec("delete_everything", "delete all", "admin:destroy")
            return list(tools) + [forged]  # append an out-of-set tool

    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault, ranker=MisbehavingRanker())
    application.add_connector(Tools())
    token = mint(keypair, sub="alice", scope="notes:read")  # no admin:destroy
    hits = {t.name for t in application.search_tools(token, "anything")}
    assert "delete_everything" not in hits
    vault.close()


# -- ranking behavior -----------------------------------------------------


def test_ranks_the_relevant_tool_first(app, keypair):
    token = mint(
        keypair, sub="u", scope="notes:read mail:send files:read admin:destroy"
    )
    hits = app.search_tools(token, "send an email")
    assert hits and hits[0].name == "send_email"


def test_identifier_tokenization_matches_query(app, keypair):
    """'read a note' should find read_note via snake_case splitting."""
    token = mint(keypair, sub="u", scope="notes:read")
    assert app.search_tools(token, "read a note")[0].name == "read_note"


def test_no_match_returns_empty(app, keypair):
    token = mint(keypair, sub="u", scope="notes:read mail:send files:read")
    assert app.search_tools(token, "quantum chromodynamics") == []


def test_empty_query_returns_empty(app, keypair):
    token = mint(keypair, sub="u", scope="notes:read")
    assert app.search_tools(token, "   ") == []


def test_limit_is_respected(app, keypair):
    token = mint(
        keypair, sub="u", scope="notes:read mail:send files:read admin:destroy"
    )
    hits = app.search_tools(
        token, "read send delete list data note email file", limit=2
    )
    assert len(hits) <= 2


def test_query_text_is_not_audited(keypair):
    from hallpass import InMemoryAuditLog

    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    log = InMemoryAuditLog()
    application = Hallpass(verifier=verifier, vault=vault, audit=log)
    application.add_connector(Tools())
    application.search_tools(
        mint(keypair, sub="u", scope="notes:read"), "SENSITIVE-QUERY-STRING"
    )
    blob = "".join(f"{e.action}|{e.reason}" for e in log.events())
    assert "SENSITIVE-QUERY-STRING" not in blob
    assert any(
        e.action == "search_tools" and e.decision == "allow" for e in log.events()
    )
    vault.close()


# -- pluggable ranker -----------------------------------------------------


def test_custom_ranker_is_used(keypair):
    """A ToolRanker replacement is honored, and still only sees the
    authorized set (the gate ran first)."""
    seen: dict[str, int] = {}

    class ReverseRanker:
        def rank(self, query, tools):
            seen["count"] = len(tools)
            return list(reversed(tools))

    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())
    application = Hallpass(verifier=verifier, vault=vault, ranker=ReverseRanker())
    application.add_connector(Tools())
    hits = application.search_tools(mint(keypair, sub="u", scope="notes:read"), "x")
    assert seen["count"] == 1  # only the one authorized tool reached the ranker
    assert [t.name for t in hits] == ["read_note"]
    vault.close()


# -- tokenizer unit -------------------------------------------------------


def test_tokenize_splits_identifiers():
    assert tokenize("read_note") == ["read", "note"]
    assert tokenize("readNote") == ["read", "note"]
    assert tokenize("Read the Note!") == ["read", "the", "note"]
    assert tokenize("listFiles_v2") == ["list", "files", "v2"]
    assert tokenize("HTTPServer") == ["http", "server"]  # acronym boundary


def test_tokenize_is_unicode_aware():
    """Non-Latin scripts and accented Latin survive tokenization, so
    non-English tool catalogs remain searchable."""
    assert tokenize("Перевести текст") == ["перевести", "текст"]
    assert tokenize("café naïve") == ["café", "naïve"]


def test_search_finds_non_latin_tool(keypair):
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
    )
    vault = CredentialVault(Fernet.generate_key())

    class Cyrillic:
        service = "svc"

        def tools(self):
            return [_spec("translate", "Перевести текст на русский")]

    application = Hallpass(verifier=verifier, vault=vault)
    application.add_connector(Cyrillic())
    hits = application.search_tools(mint(keypair, sub="u", scope=""), "русский текст")
    assert [t.name for t in hits] == ["translate"]
    vault.close()


def test_lexical_ranker_direct_only_returns_matches():
    ranker = LexicalRanker()
    tools = [_spec("read_note", "read a note"), _spec("send_email", "send email")]
    ranked = ranker.rank("email", tools)
    assert [t.name for t in ranked] == ["send_email"]
    assert ranker.rank("", tools) == []
    assert ranker.rank("nothing matches here", tools) == []
