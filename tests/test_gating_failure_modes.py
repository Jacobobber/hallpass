"""Every test names a way scope gating goes wrong, and proves it can't
here. The one that matters most: gating holds at CALL time, because
hiding a tool from a menu is cosmetics, not security."""

import pytest

from hallpass import Principal, ToolDenied, ToolGate, ToolSpec, UnknownTool


def spec(name: str, *scopes: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        required_scopes=frozenset(scopes),
        handler=lambda ctx, **kw: name,
    )


def principal(*scopes: str) -> Principal:
    return Principal(subject="alice", scopes=frozenset(scopes))


@pytest.fixture()
def gate():
    g = ToolGate()
    g.register(spec("public_ping"))
    g.register(spec("read_notes", "notes:read"))
    g.register(spec("delete_everything", "admin:destroy"))
    return g


def test_ungranted_tool_absent_from_catalog(gate):
    names = {t.name for t in gate.catalog(principal("notes:read"))}
    assert names == {"public_ping", "read_notes"}
    assert "delete_everything" not in names


def test_gating_enforced_at_call_not_just_listing(gate):
    """A client that skips list_tools and calls directly gets refused.
    The catalog is a view; authorize() is the boundary."""
    with pytest.raises(ToolDenied):
        gate.authorize(principal("notes:read"), "delete_everything")


def test_no_scopes_sees_only_public_tools(gate):
    assert {t.name for t in gate.catalog(principal())} == {"public_ping"}


def test_unknown_tool_refused(gate):
    with pytest.raises(UnknownTool):
        gate.authorize(principal("notes:read"), "no_such_tool")


def test_partial_scopes_do_not_unlock(gate):
    """Requiring two scopes means both; one of two is zero of two."""
    g = ToolGate()
    g.register(spec("export", "data:read", "data:export"))
    assert g.catalog(principal("data:read")) == []
    with pytest.raises(ToolDenied):
        g.authorize(principal("data:read"), "export")
    assert {t.name for t in g.catalog(principal("data:read", "data:export"))} == {
        "export"
    }


def test_two_principals_get_distinct_catalogs(gate):
    reader = {t.name for t in gate.catalog(principal("notes:read"))}
    admin = {t.name for t in gate.catalog(principal("admin:destroy"))}
    assert "read_notes" in reader and "read_notes" not in admin
    assert "delete_everything" in admin and "delete_everything" not in reader


def test_duplicate_registration_refused(gate):
    with pytest.raises(ValueError):
        gate.register(spec("public_ping"))


def test_denial_names_missing_scopes_not_granted_ones(gate):
    """The error helps the caller fix their grant without disclosing what
    else the caller already holds."""
    try:
        gate.authorize(
            principal("notes:read", "some:private:scope"), "delete_everything"
        )
    except ToolDenied as err:
        assert "admin:destroy" in str(err)
        assert "some:private:scope" not in str(err)
