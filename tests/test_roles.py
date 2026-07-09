"""Roles: named capability sets assigned to principals; a subject's effective
scopes are the union of its roles. Run over both the in-memory and durable
stores. Each test names the property it pins."""

import pytest

from hallpass import (
    InMemoryRoleStore,
    Role,
    RoleError,
    SqliteRoleStore,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRoleStore()
    else:
        s = SqliteRoleStore(path=str(tmp_path / "roles.db"))
        yield s
        s.close()


def test_effective_scopes_is_union_of_assigned_roles(store):
    store.define(Role("reviewer", frozenset({"github:read"})))
    store.define(Role("releaser", frozenset({"github:write", "ci:run"})))
    store.assign("alice", "reviewer")
    store.assign("alice", "releaser")
    assert store.scopes_for("alice") == frozenset(
        {"github:read", "github:write", "ci:run"}
    )
    assert store.roles("alice") == ["releaser", "reviewer"]  # sorted


def test_subject_with_no_roles_has_no_scopes(store):
    assert store.scopes_for("nobody") == frozenset()
    assert store.roles("nobody") == []


def test_assigning_undefined_role_raises(store):
    with pytest.raises(RoleError, match="role 'ghost' is not defined"):
        store.assign("alice", "ghost")
    # RoleError is a KeyError subclass, so existing handlers still catch it
    assert isinstance(RoleError("x"), KeyError)


def test_unassign(store):
    store.define(Role("reviewer", frozenset({"github:read"})))
    store.assign("alice", "reviewer")
    assert store.unassign("alice", "reviewer") is True
    assert store.scopes_for("alice") == frozenset()
    assert store.unassign("alice", "reviewer") is False  # already gone


def test_redefining_a_role_changes_effective_scopes(store):
    """An org change is a role change: widen a role and every holder's effective
    scopes follow, without touching assignments."""
    store.define(Role("reviewer", frozenset({"github:read"})))
    store.assign("alice", "reviewer")
    store.assign("bob", "reviewer")
    store.define(Role("reviewer", frozenset({"github:read", "github:write"})))
    assert store.scopes_for("alice") == frozenset({"github:read", "github:write"})
    assert store.scopes_for("bob") == frozenset({"github:read", "github:write"})


def test_roles_are_per_subject(store):
    store.define(Role("admin", frozenset({"admin:all"})))
    store.assign("alice", "admin")
    assert store.scopes_for("alice") == frozenset({"admin:all"})
    assert store.scopes_for("bob") == frozenset()  # bob does not inherit alice's role


def test_sqlite_roles_are_durable(tmp_path):
    """Definitions and assignments survive a fresh store on the same file."""
    path = str(tmp_path / "r.db")
    s = SqliteRoleStore(path=path)
    s.define(Role("reviewer", frozenset({"github:read"})))
    s.assign("alice", "reviewer")
    s.close()

    reopened = SqliteRoleStore(path=path)
    assert reopened.scopes_for("alice") == frozenset({"github:read"})
    assert reopened.roles("alice") == ["reviewer"]
    reopened.close()


def test_roles_feed_minting(store):
    """The intended use: resolve a subject's effective scopes, mint with exactly
    those. Here we just show the resolution drives a scoped token via dev_app."""
    from hallpass import dev_app

    _, token = dev_app()
    store.define(Role("reviewer", frozenset({"github:read"})))
    store.assign("bot-1", "reviewer")
    scopes = store.scopes_for("bot-1")
    tok = token("bot-1", scopes)  # mint with the role-derived scopes
    assert tok  # a real RS256 token; scopes came from the role, not hand-typed
