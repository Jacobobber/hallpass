"""Mint each agent its own service credential.

A ``Team``'s ``mint`` is a callable ``(subject, scopes) -> token``. In
development that is ``dev_app``'s in-process signer; in production each agent
should obtain its OWN service token from your identity provider's OAuth 2.0
client-credentials grant -- one OAuth client per agent -- so the token an agent
carries is issued *to the agent*, never borrowed from a human's session and
never a shared secret. ``ClientCredentialsMinter`` is that path: a callable
``AgentMinter`` that exchanges an agent's client credentials for a scoped
service token at the IdP's token endpoint.

The verifier still does the enforcing: pair the minted token with a
``ProvisioningGuard`` so the agent is proven a *service* principal whose subject
is its own name with exactly its harness scopes before it launches.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .oauth import HttpxTokenClient, OAuthError, TokenHttp

__all__ = ["AgentMinter", "AgentClient", "ClientCredentialsMinter"]


class AgentMinter(Protocol):
    """Turns ``(subject, scopes)`` into a token the hallpass verifier accepts.
    A plain callable satisfies it, so a dev signer or a lambda drops straight
    into ``Team(mint=...)``; the named protocol is the production shape -- one
    minted identity per agent, never a human's."""

    def __call__(self, subject: str, scopes: frozenset[str]) -> str: ...


@dataclass(frozen=True)
class AgentClient:
    """One agent's OAuth client-credentials identity at the IdP. The secret is
    the agent's own; it is sent only to the token endpoint and never logged."""

    client_id: str
    client_secret: str


class ClientCredentialsMinter:
    """Mint each agent its own service token via the OAuth 2.0
    client-credentials grant. Each agent is a distinct OAuth client (its own
    ``client_id`` / ``client_secret``) at your IdP; minting POSTs
    ``grant_type=client_credentials`` with that agent's credentials and the
    requested scope to the token endpoint and returns the access token. So the
    token an agent carries is issued to the agent, not shared and not a human's.
    An agent with no registered client is refused -- there is no silent fallback
    to a shared identity."""

    def __init__(
        self,
        *,
        token_url: str,
        clients: Mapping[str, AgentClient] | None = None,
        audience: str | None = None,
        http: TokenHttp | None = None,
    ) -> None:
        self._token_url = token_url
        self._clients: dict[str, AgentClient] = dict(clients or {})
        self._audience = audience
        self._http = http or HttpxTokenClient()

    def register(self, subject: str, client: AgentClient) -> None:
        """Add or replace an agent's client-credentials identity."""
        self._clients[subject] = client

    def __call__(self, subject: str, scopes: Iterable[str]) -> str:
        client = self._clients.get(subject)
        if client is None:
            raise OAuthError(
                f"no client-credentials identity registered for agent {subject!r}; "
                "register one before spawning it -- each agent is its own OAuth "
                "client, never a shared or human identity"
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "scope": " ".join(scopes),
        }
        if self._audience is not None:
            data["audience"] = self._audience
        tokens = self._http.post_form(
            self._token_url, data=data, headers={"Accept": "application/json"}
        )
        access = tokens.get("access_token")
        if not access:
            raise OAuthError(
                f"token endpoint returned no access_token for agent {subject!r}"
            )
        return str(access)
