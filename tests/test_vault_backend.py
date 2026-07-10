"""The vault backend seam: CredentialVault owns encryption; a VaultBackend holds
ciphertext, so the store can be swapped without moving the encryption boundary.
Each test names the property it pins."""

import pytest
from cryptography.fernet import Fernet

from hallpass import (
    CredentialVault,
    InMemoryVaultBackend,
    SqliteVaultBackend,
    VaultError,
)


@pytest.fixture(params=["default", "sqlite-backend", "memory-backend"])
def vault(request, tmp_path):
    key = Fernet.generate_key()
    if request.param == "default":
        v = CredentialVault(key)  # SqliteVaultBackend(:memory:) under the hood
    elif request.param == "sqlite-backend":
        v = CredentialVault(
            key, backend=SqliteVaultBackend(path=str(tmp_path / "v.db"))
        )
    else:
        v = CredentialVault(key, backend=InMemoryVaultBackend())
    yield v
    v.close()


def test_store_fetch_roundtrip(vault):
    vault.store("alice", "github", "ghp_secret")
    assert vault.fetch("alice", "github") == "ghp_secret"
    assert vault.fetch("alice", "slack") is None  # not connected


def test_cross_subject_isolation(vault):
    vault.store("alice", "github", "alice-token")
    vault.store("bob", "github", "bob-token")
    assert vault.fetch("alice", "github") == "alice-token"
    assert vault.fetch("bob", "github") == "bob-token"


def test_delete_and_services(vault):
    vault.store("alice", "github", "x")
    vault.store("alice", "slack", "y")
    assert vault.services("alice") == ["github", "slack"]  # sorted, names only
    assert vault.delete("alice", "github") is True
    assert vault.services("alice") == ["slack"]
    assert vault.delete("alice", "github") is False


def test_backend_only_sees_ciphertext(vault):
    """The plaintext never reaches the backend -- the encryption boundary stays
    in CredentialVault regardless of where the bytes are stored."""
    vault.store("alice", "github", "PLAINTEXT_SECRET")
    stored = vault._backend.get("alice", "github")
    assert isinstance(stored, bytes)
    assert b"PLAINTEXT_SECRET" not in stored  # it's Fernet ciphertext, not the secret


def test_wrong_key_raises_vaulterror_without_leaking():
    backend = InMemoryVaultBackend()
    CredentialVault(Fernet.generate_key(), backend=backend).store(
        "a", "svc", "SUPER_SECRET_VALUE"
    )
    other = CredentialVault(Fernet.generate_key(), backend=backend)  # different key
    with pytest.raises(VaultError) as e:
        other.fetch("a", "svc")
    assert "svc" in str(e.value)  # names the service (actionable)
    assert "SUPER_SECRET_VALUE" not in str(e.value)  # never the secret


def test_durable_reflects_backend():
    assert (
        CredentialVault(Fernet.generate_key(), backend=InMemoryVaultBackend()).durable
        is False
    )
    assert CredentialVault(Fernet.generate_key(), path=":memory:").durable is False


def test_sqlite_backend_is_durable_and_shareable(tmp_path):
    """The point of the seam: two vaults over the same backend/file see the same
    credentials, and they survive a restart."""
    key = Fernet.generate_key()
    path = str(tmp_path / "v.db")
    v1 = CredentialVault(key, backend=SqliteVaultBackend(path=path))
    v1.store("alice", "github", "shared")
    assert v1.durable is True
    v1.close()
    # a fresh vault (same key + file) reads it back -- durable across "restart"
    v2 = CredentialVault(key, backend=SqliteVaultBackend(path=path))
    assert v2.fetch("alice", "github") == "shared"
    v2.close()


def test_repr_never_leaks(vault):
    vault.store("alice", "github", "top-secret")
    assert repr(vault) == "CredentialVault(<locked>)"
    assert "top-secret" not in repr(vault)
