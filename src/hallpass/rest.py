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


# Auth style on RestService.auth:
#   "bearer" -> Authorization: Bearer <cred>
#   "token"  -> Authorization: token <cred>
#   "bot"    -> Authorization: Bot <cred>
#   "basic"  -> Authorization: Basic <cred>   (cred is pre-encoded base64)
#   ("header", name) -> send the raw credential in header `name`
#   ("query",  name) -> send the raw credential as query parameter `name`


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
    auth: str | tuple[str, str] = "bearer"
    headers: dict[str, str] = field(default_factory=dict)
    # True for services with a per-tenant host (Jira, Zendesk, Salesforce);
    # the base URL must be supplied at load time via base_url=.
    requires_base_url: bool = False


def _apply_auth(
    service: RestService, credential: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (extra headers, extra query params) that carry the credential
    in the service's auth style."""
    auth = service.auth
    if isinstance(auth, tuple):
        kind, name = auth
        if kind == "header":
            return {name: credential}, {}
        if kind == "query":
            return {}, {name: credential}
        raise ConnectorError(f"unknown auth tuple kind: {kind!r}")
    if auth == "bearer":
        return {"Authorization": f"Bearer {credential}"}, {}
    if auth == "token":
        return {"Authorization": f"token {credential}"}, {}
    if auth == "bot":
        return {"Authorization": f"Bot {credential}"}, {}
    if auth == "basic":
        return {"Authorization": f"Basic {credential}"}, {}
    raise ConnectorError(f"unknown auth style: {auth!r}")


def _make_handler(
    service: RestService, endpoint: Endpoint, http: HttpClient, base_url: str
) -> Callable[..., Any]:
    def handler(ctx: UserContext, **args: Any) -> Any:
        credential = ctx.credential()
        if credential is None:
            raise ConnectorError(f"{service.service} is not connected for this user")
        try:
            path = endpoint.path.format(**args)
        except KeyError as exc:
            raise ConnectorError(f"missing path argument {exc}") from None
        url = base_url.rstrip("/") + path
        auth_headers, auth_params = _apply_auth(service, credential)
        headers = {**service.headers, **auth_headers}
        params = {k: args[k] for k in endpoint.query if k in args}
        params.update(auth_params)
        body = {k: args[k] for k in endpoint.body if k in args} or None
        return http.request(
            endpoint.method, url, headers=headers, params=params, json=body
        )

    return handler


class RestConnector:
    """A hallpass Connector built from a RestService description. Plug it
    into ``Hallpass.add_connector`` like any other connector. For a
    per-tenant service (``requires_base_url``), pass ``base_url`` with the
    tenant's host."""

    def __init__(
        self,
        spec: RestService,
        *,
        http: HttpClient | None = None,
        available: Callable[[], bool] | None = None,
        base_url: str | None = None,
    ) -> None:
        if spec.requires_base_url and not base_url:
            raise ValueError(
                f"{spec.service} needs a per-tenant base_url "
                "(e.g. https://your-site.example.com)"
            )
        self.service = spec.service
        self._spec = spec
        self._http = http or HttpxClient()
        self._available = available
        self._base_url = base_url or spec.base_url

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=endpoint.name,
                description=endpoint.description,
                required_scopes=endpoint.scopes,
                handler=_make_handler(self._spec, endpoint, self._http, self._base_url),
                connector=self._spec.service,
                input_schema=endpoint.input_schema(),
            )
            for endpoint in self._spec.endpoints
        ]

    def available(self) -> bool:
        return self._available() if self._available is not None else True
