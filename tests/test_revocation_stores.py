"""Fleet-wide revocation: a durable shared source plus a TTL cache on the verify
hot path. These pin the Sqlite source (durable, cross-instance), and that the
cache serves O(1) reads with write-through-immediate plus source-side changes
picked up within the TTL -- the eventual-consistency the fleet relies on."""

import pytest

from hallpass import (
    CachedRevocationList,
    InMemoryRevocationList,
    SqliteRevocationList,
    StaticJwks,
    TokenVerifier,
    VerificationError,
)

from conftest import AUDIENCE, ISSUER, jwk_for, mint


def test_sqlite_source_is_durable(tmp_path):
    path = str(tmp_path / "rev.db")
    store = SqliteRevocationList(path=path)
    store.revoke("agent-7", reason="compromised")
    assert store.is_revoked("agent-7") and store.revoked() == ["agent-7"]
    store.close()
    # a fresh instance on the same file sees it (a second replica / a restart)
    store2 = SqliteRevocationList(path=path)
    assert store2.is_revoked("agent-7")
    store2.restore("agent-7")
    assert not store2.is_revoked("agent-7") and store2.revoked() == []
    store2.close()


def test_cache_write_through_is_immediate():
    source = InMemoryRevocationList()
    clock = {"t": 0.0}
    cached = CachedRevocationList(source, ttl_seconds=5.0, now=lambda: clock["t"])
    cached.revoke("agent-7")  # through the wrapper
    # visible at once on this replica, no clock advance, and it reached the source
    assert cached.is_revoked("agent-7")
    assert source.is_revoked("agent-7")


def test_cache_picks_up_source_change_within_ttl():
    """A revoke on ANOTHER replica (written straight to the shared source) is not
    seen until the cache refreshes -- then it is. This is the bounded fleet
    propagation."""
    source = InMemoryRevocationList()
    clock = {"t": 100.0}
    cached = CachedRevocationList(source, ttl_seconds=5.0, now=lambda: clock["t"])
    assert not cached.is_revoked("agent-7")  # primes the (empty) cache
    source.revoke("agent-7")  # a different replica revokes, straight to the source
    assert not cached.is_revoked("agent-7")  # still within TTL -> stale view
    clock["t"] += 6.0  # TTL elapses
    assert cached.is_revoked("agent-7")  # refresh picks it up


def test_verifier_with_cached_sqlite_revocation(keypair, tmp_path):
    source = SqliteRevocationList(path=str(tmp_path / "rev.db"))
    cached = CachedRevocationList(source, ttl_seconds=5.0)
    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=StaticJwks({"keys": [jwk_for(keypair, "k1")]}),
        revocations=cached,
    )
    token = mint(keypair, sub="agent-7", scope="x")
    assert verifier.verify(token).subject == "agent-7"
    cached.revoke("agent-7")  # write-through -> immediate on this verifier
    with pytest.raises(VerificationError):
        verifier.verify(token)
    source.close()
