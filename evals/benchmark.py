"""Throughput micro-benchmarks for hallpass's hot paths.

"Competitive performance" should be a measured claim, not a vibe. This times the
operations that run on every request: token verification (with and without the
verify cache), call-time gating, the FLEX codec, and the vault, plus a
loopback comparison of the pooled vs. an unpooled HTTP client so the
connection-reuse win is visible in numbers.

Run it:  python evals/benchmark.py

Numbers are ops/sec on the machine you run it on; use them for relative
comparisons (cached vs uncached, pooled vs unpooled), not as absolute promises.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

import jwt

from hallpass import (
    ChannelPolicy,  # noqa: F401  (kept for parity with the coordination bench)
    CredentialVault,
    Principal,
    ToolGate,
    ToolSpec,
    flex,
)
from hallpass.identity import StaticJwks, TokenVerifier

ISSUER = "https://idp.example.test"
AUDIENCE = "https://tools.example.test"


def bench(fn: Callable[[], object], *, seconds: float = 0.3) -> float:
    """Run fn repeatedly for ~seconds; return ops/sec."""
    # warm up
    fn()
    count = 0
    start = time.perf_counter()
    while time.perf_counter() - start < seconds:
        fn()
        count += 1
    elapsed = time.perf_counter() - start
    return count / elapsed if elapsed else 0.0


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", use="sig", alg="RS256")
    return key, StaticJwks({"keys": [jwk]})


def _mint(key) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "alice",
            "scope": "a:read b:read",
            "iat": now,
            "exp": now + 3600,
        },
        key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


def run(seconds: float = 0.3) -> dict[str, float]:
    key, jwks = _keypair()
    token = _mint(key)

    cached = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks)
    uncached = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, cache_size=0)

    gate = ToolGate()
    gate.register(
        ToolSpec("t", "d", frozenset({"a:read"}), handler=lambda ctx, **k: None)
    )
    principal = Principal("alice", frozenset({"a:read", "b:read"}))

    vault = CredentialVault(Fernet.generate_key())
    vault.store("alice", "svc", "secret-value")

    msg = flex.Message(
        kind="task", to=("w",), refs=("t1",), fields={"do": "x"}, note="hello there"
    )
    wire = flex.encode(msg)

    results = {
        "verify (cached)": bench(lambda: cached.verify(token), seconds=seconds),
        "verify (uncached)": bench(lambda: uncached.verify(token), seconds=seconds),
        "gate.authorize": bench(
            lambda: gate.authorize(principal, "t"), seconds=seconds
        ),
        "flex.encode": bench(lambda: flex.encode(msg), seconds=seconds),
        "flex.parse": bench(lambda: flex.parse(wire), seconds=seconds),
        "vault.fetch": bench(lambda: vault.fetch("alice", "svc"), seconds=seconds),
    }
    vault.close()
    return results


def run_http(requests: int = 200) -> dict[str, float] | None:
    """Pooled vs. unpooled HTTP against a loopback server. Returns ops/sec for
    each, or None if httpx isn't installed. Uses a real socket on localhost."""
    try:
        import httpx
    except ImportError:
        return None
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        client = httpx.Client()

        def pooled():
            client.get(url)

        def unpooled():
            with httpx.Client() as c:  # fresh connection every call
                c.get(url)

        def ops(fn):
            start = time.perf_counter()
            for _ in range(requests):
                fn()
            return requests / (time.perf_counter() - start)

        pooled()  # warm
        out = {"http pooled": ops(pooled), "http unpooled": ops(unpooled)}
        client.close()
        return out
    finally:
        server.shutdown()


def main() -> int:
    print("hallpass micro-benchmarks (ops/sec, higher is better)\n")
    for name, ops in run().items():
        print(f"  {name:20s} {ops:12,.0f}")
    http = run_http()
    if http:
        print()
        for name, ops in http.items():
            print(f"  {name:20s} {ops:12,.0f}")
        speedup = http["http pooled"] / max(http["http unpooled"], 1e-9)
        print(f"\n  pooled is {speedup:.1f}x the unpooled client on loopback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
