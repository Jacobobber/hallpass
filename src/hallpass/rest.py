"""Declarative REST connectors: a connector is data, not code.

A service is described by its base URL, an auth style, and a list of
endpoints. Each endpoint becomes a gated hallpass tool whose handler builds
the request, attaches the calling user's vaulted credential, and returns the
parsed response. There are no per-vendor SDKs: every connector is the same
thin HTTP wrapper over the service's own REST API, which is what makes a
large catalog tractable (see ``hallpass.catalog``).

The HTTP client is injected (``HttpClient``), so the connectors are fully
testable against a fake transport and never touch the network in tests. The
default client uses ``httpx`` (the ``connectors`` extra).

Auth model: the per-user credential lives in the vault (a PAT or an OAuth
access token for the service), and the connector sends it in the style the
service expects. hallpass does not yet perform the OAuth dance to each
provider; getting the token into the vault is the operator's job for now
(a per-provider connect flow is on the roadmap).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .connectors import UserContext
from .gating import ToolSpec

__all__ = [
    "Endpoint",
    "RestService",
    "RestConnector",
    "HttpClient",
    "HttpxClient",
    "ConnectorError",
]

_PATH_PARAM = re.compile(r"\{(\w+)\}")


class ConnectorError(Exception):
    """A connector could not complete a call: the user has not connected the
    service, a path argument is missing, or the service returned an error.
    The message is safe to surface; it never contains the credential."""


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        json: dict[str, Any] | None,
    ) -> Any:
        """Perform the request and return the parsed response body (dict,
        list, or str). Raise ConnectorError on a non-success status."""
        ...


class HttpxClient:
    """Default HttpClient over httpx (the ``connectors`` extra). Import is
    deferred so the core has no httpx dependency."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        json: dict[str, Any] | None,
    ) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ConnectorError(
                "prewired connectors need the 'connectors' extra: "
                "pip install 'hallpass[connectors]'"
            ) from exc
        response = httpx.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise ConnectorError(f"{method} {url} failed: HTTP {response.status_code}")
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return response.json()
        return response.text


# Auth style on RestService.auth: "bearer" -> Authorization: Bearer <cred>;
# "token" -> Authorization: token <cred>; "bot" -> Authorization: Bot <cred>;
# or a (header_name,) tuple to send the raw credential in a custom header.


@dataclass(frozen=True)
class Endpoint:
    name: str
    description: str
    method: str
    path: str
    scopes: frozenset[str] = frozenset()
    query: tuple[str, ...] = ()  # tool args passed as query parameters
    body: tuple[str, ...] = ()  # tool args passed as JSON body fields
    required: frozenset[str] = frozenset()  # required args beyond path params

    def path_params(self) -> list[str]:
        return _PATH_PARAM.findall(self.path)

    def input_schema(self) -> dict[str, Any]:
        path_params = self.path_params()
        required = set(path_params) | set(self.required)
        properties: dict[str, Any] = {}
        for name in [*path_params, *self.query, *self.body]:
            properties[name] = {"type": "string"}
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = sorted(required)
        return schema


@dataclass(frozen=True)
class RestService:
    service: str
    base_url: str
    endpoints: tuple[Endpoint, ...]
    auth: str | tuple[str] = "bearer"
    headers: dict[str, str] = field(default_factory=dict)


def _auth_headers(service: RestService, credential: str) -> dict[str, str]:
    auth = service.auth
    if isinstance(auth, tuple):
        return {auth[0]: credential}
    if auth == "bearer":
        return {"Authorization": f"Bearer {credential}"}
    if auth == "token":
        return {"Authorization": f"token {credential}"}
    if auth == "bot":
        return {"Authorization": f"Bot {credential}"}
    raise ConnectorError(f"unknown auth style: {auth!r}")


def _make_handler(
    service: RestService, endpoint: Endpoint, http: HttpClient
) -> Callable[..., Any]:
    def handler(ctx: UserContext, **args: Any) -> Any:
        credential = ctx.credential()
        if credential is None:
            raise ConnectorError(f"{service.service} is not connected for this user")
        try:
            path = endpoint.path.format(**args)
        except KeyError as exc:
            raise ConnectorError(f"missing path argument {exc}") from None
        url = service.base_url.rstrip("/") + path
        headers = {**service.headers, **_auth_headers(service, credential)}
        params = {k: args[k] for k in endpoint.query if k in args}
        body = {k: args[k] for k in endpoint.body if k in args} or None
        return http.request(
            endpoint.method, url, headers=headers, params=params, json=body
        )

    return handler


class RestConnector:
    """A hallpass Connector built from a RestService description. Plug it
    into ``Hallpass.add_connector`` like any other connector."""

    def __init__(
        self,
        spec: RestService,
        *,
        http: HttpClient | None = None,
        available: Callable[[], bool] | None = None,
    ) -> None:
        self.service = spec.service
        self._spec = spec
        self._http = http or HttpxClient()
        self._available = available

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=endpoint.name,
                description=endpoint.description,
                required_scopes=endpoint.scopes,
                handler=_make_handler(self._spec, endpoint, self._http),
                connector=self._spec.service,
                input_schema=endpoint.input_schema(),
            )
            for endpoint in self._spec.endpoints
        ]

    def available(self) -> bool:
        return self._available() if self._available is not None else True
