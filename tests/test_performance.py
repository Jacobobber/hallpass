"""Guards for the two hot-path performance changes: the verify cache must
actually skip re-verification for a repeated token (and honour cache_size=0),
and the httpx client must reuse one pooled connection. Correctness first --
the benchmark itself (evals/benchmark.py) is the measured story; these pin the
behaviour that makes it fast so it can't silently regress."""

import sys
from pathlib import Path

from hallpass import HttpxClient
from hallpass.identity import StaticJwks, TokenVerifier

from conftest import AUDIENCE, ISSUER, jwk_for, mint

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for evals.benchmark


class CountingJwks:
    """Wraps a JwksSource and counts how often the keys are fetched -- one
    fetch per full verification, zero on a cache hit."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def get(self, *, force_refresh=False):
        self.calls += 1
        return self.inner.get(force_refresh=force_refresh)


def _verifier(keypair, jwks, **kw):
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, **kw)


def test_verify_cache_skips_reverification(keypair):
    jwks = CountingJwks(StaticJwks({"keys": [jwk_for(keypair, "k1")]}))
    verifier = _verifier(keypair, jwks)
    token = mint(keypair, sub="alice", scope="a:read")

    p1 = verifier.verify(token)
    p2 = verifier.verify(token)  # cache hit: no key fetch, no RSA verify
    assert p1 == p2
    assert jwks.calls == 1  # only the first verification touched the keys


def test_cache_can_be_disabled(keypair):
    jwks = CountingJwks(StaticJwks({"keys": [jwk_for(keypair, "k1")]}))
    verifier = _verifier(keypair, jwks, cache_size=0)
    token = mint(keypair, sub="alice", scope="a:read")

    verifier.verify(token)
    verifier.verify(token)
    assert jwks.calls == 2  # every call verifies fully when the cache is off


def test_cache_still_verifies_a_different_token(keypair):
    jwks = CountingJwks(StaticJwks({"keys": [jwk_for(keypair, "k1")]}))
    verifier = _verifier(keypair, jwks)
    verifier.verify(mint(keypair, sub="alice", scope="a:read"))
    verifier.verify(mint(keypair, sub="bob", scope="b:read"))  # distinct token
    assert jwks.calls == 2  # a new token is a cache miss and is fully verified


def test_httpx_client_pools_one_connection():
    client = HttpxClient()
    first = client._get_client()
    assert client._get_client() is first  # reused, not recreated per call
    client.close()
    assert client._get_client() is not first  # a fresh pool after close
    client.close()


def test_benchmark_runs_and_cache_beats_uncached():
    from evals.benchmark import run

    results = run(seconds=0.03)
    assert all(ops > 0 for ops in results.values())
    # the whole point of the cache: a hit is far cheaper than an RSA verify
    assert results["verify (cached)"] > results["verify (uncached)"]
