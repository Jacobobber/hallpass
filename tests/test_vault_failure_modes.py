"""Every test names a way multi-user credential storage leaks, and proves
this vault does not."""

import pytest
from cryptography.fernet import Fernet

from hallpass import CredentialVault, VaultError

SECRET = "hunter2-extremely-secret-api-key"


def test_secret_encrypted_at_rest(tmp_path):
    """The database file must never contain the plaintext."""
    db = tmp_path / "vault.sqlite3"
    vault = CredentialVault(Fernet.generate_key(), path=str(db))
    vault.store("alice", "notes", SECRET)
    vault.close()
    raw = db.read_bytes()
    assert SECRET.encode() not in raw
    assert b"hunter2" not in raw


def test_cross_user_isolation(vault):
    vault.store("alice", "notes", "alice-key")
    vault.store("bob", "notes", "bob-key")
    assert vault.fetch("alice", "notes") == "alice-key"
    assert vault.fetch("bob", "notes") == "bob-key"


def test_cross_service_isolation(vault):
    vault.store("alice", "notes", "notes-key")
    vault.store("alice", "crm", "crm-key")
    assert vault.fetch("alice", "notes") == "notes-key"
    assert vault.fetch("alice", "crm") == "crm-key"


def test_missing_credential_is_none_not_error(vault):
    """A user who never connected a service is a normal state."""
    assert vault.fetch("alice", "never-connected") is None


def test_wrong_key_fails_closed_without_leaking(tmp_path):
    """Key rotation without migration: decryption must fail with an error
    that carries no secret material, never return garbage."""
    db = tmp_path / "vault.sqlite3"
    first = CredentialVault(Fernet.generate_key(), path=str(db))
    first.store("alice", "notes", SECRET)
    first.close()

    second = CredentialVault(Fernet.generate_key(), path=str(db))
    with pytest.raises(VaultError) as excinfo:
        second.fetch("alice", "notes")
    assert SECRET not in str(excinfo.value)
    second.close()


def test_repr_never_shows_contents(vault):
    vault.store("alice", "notes", SECRET)
    assert SECRET not in repr(vault)


def test_overwrite_replaces(vault):
    vault.store("alice", "notes", "old")
    vault.store("alice", "notes", "new")
    assert vault.fetch("alice", "notes") == "new"


def test_delete_and_listing_names_only(vault):
    vault.store("alice", "notes", SECRET)
    vault.store("alice", "crm", "x")
    assert vault.services("alice") == ["crm", "notes"]
    assert SECRET not in "".join(vault.services("alice"))
    assert vault.delete("alice", "notes") is True
    assert vault.fetch("alice", "notes") is None
    assert vault.delete("alice", "notes") is False
