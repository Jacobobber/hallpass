"""A complete, runnable hallpass server core in one file.

No identity provider and no MCP transport required: it mints its own signed
token against an in-memory key so you can watch per-user auth, the credential
vault, and scope gating work end to end. Runs on the core install alone
(`pip install hallpass`), no extras.

    python examples/minimal.py
"""

import json
import time

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from hallpass import CredentialVault, Hallpass, StaticJwks, TokenVerifier, ToolSpec

ISSUER = "https://demo-idp.example"
AUDIENCE = "https://demo-server.example"


def make_verifier(private_key):
    """A StaticJwks stands in for an OIDC provider's published keys; the
    verification logic is identical to production (which fetches over HTTPS)."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid="k1", use="sig", alg="RS256")
    return TokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks=StaticJwks({"keys": [jwk]})
    )


def mint(private_key, subject, scope):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "scope": scope,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "k1"})


class NotesConnector:
    service = "notes"

    def tools(self):
        return [
            ToolSpec(
                name="read_note",
                description="Read the calling user's note",
                required_scopes=frozenset({"notes:read"}),
                handler=self._read,
            )
        ]

    def _read(self, ctx, **kwargs):
        cred = ctx.credential()
        if cred is None:
            return f"{ctx.principal.subject}: notes not connected"
        return f"{ctx.principal.subject}: note read with {cred}"


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    vault = CredentialVault(Fernet.generate_key())  # bring your own key management
    app = Hallpass(verifier=make_verifier(key), vault=vault)
    app.add_connector(NotesConnector())

    # Alice has notes:read and a stored credential.
    vault.store("alice", "notes", "alice-notes-key")
    alice = mint(key, "alice", "notes:read")
    print("alice catalog:", [t.name for t in app.list_tools(alice)])
    print("alice call:   ", app.call_tool(alice, "read_note", {}))

    # Bob is authenticated but has no scope: the tool is not in his catalog,
    # and calling it anyway is refused (and looks like it does not exist).
    bob = mint(key, "bob", "")
    print("bob catalog:  ", [t.name for t in app.list_tools(bob)])
    try:
        app.call_tool(bob, "read_note", {})
    except Exception as exc:  # noqa: BLE001 - demo
        print("bob call:     refused ->", exc)

    vault.close()


if __name__ == "__main__":
    main()
