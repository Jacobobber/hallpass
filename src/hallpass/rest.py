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
service expects. ``hallpass.oauth.OAuthConnect`` runs the per-provider connect
flow that puts the token there; wiring ``OAuthConnect.attach_refresh`` into a
connector makes a stale token self-heal (a 401/403 renews and retries once).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .connectors import UserContext
from .gating import ToolSpec
from .guard import guard_response

__all__ = [
    "Endpoint",
    "RestService",
    "RestConnector",
    "HttpClient",
    "HttpxClient",
    "RetryingHttpClient",
    "RetryPolicy",
    "ConnectorError",
    "TokenRefresher",
]

_PATH_PARAM = re.compile(r"\{(\w+)\}")

# Renew (subject, service)'s stored token in the vault; OAuthConnect.refresh
# fits this shape. Wired via RestConnector.set_auto_refresh for seamless retry.
TokenRefresher = Callable[[str, str], object]


class ConnectorError(Exception):
    """A connector could not complete a call: the user has not connected the
    service, a path argument is missing, or the service returned an error.
    The message is safe to surface; it never contains the credential.

    ``status`` carries the HTTP status when the failure came from the service,
    so callers (and the auto-refresh path) can tell 401/403 apart from a 500.
    ``retry_after`` carries the service's Retry-After hint (seconds) when it
    sent one, so the retry path can wait exactly as long as asked."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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
            raise ConnectorError(
                f"{method} {url} failed: HTTP {response.status_code}",
                status=response.status_code,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return response.json()
        return response.text


def _parse_retry_after(value: str | None) -> float | None:
    """A Retry-After header in the integer-seconds form. The HTTP-date form is
    left to the backoff schedule (parsing it needs the current time)."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


@dataclass(frozen=True)
class RetryPolicy:
    """When to retry a transient failure and how long to wait. Defaults retry
    the usual transient statuses a couple of times with exponential backoff."""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 30.0
    statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class RetryingHttpClient:
    """Wrap any HttpClient so transient failures (429, 5xx) retry with backoff,
    honoring the service's Retry-After when it sends one. Auth failures
    (401/403) are deliberately NOT in the default set: those are the connector
    auto-refresh's job, not a blind retry. The clock is injected, so the retry
    schedule is exercised in tests without real waiting."""

    def __init__(
        self,
        inner: HttpClient,
        *,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._sleep = sleep

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        json: dict[str, Any] | None,
    ) -> Any:
        policy = self._policy
        attempt = 0
        while True:
            try:
                return self._inner.request(
                    method, url, headers=headers, params=params, json=json
                )
            except ConnectorError as exc:
                if exc.status not in policy.statuses or attempt >= policy.max_retries:
                    raise
                backoff = min(policy.base_delay * (2**attempt), policy.max_delay)
                self._sleep(exc.retry_after if exc.retry_after is not None else backoff)
                attempt += 1


# Auth style on RestService.auth:
#   "bearer" -> Authorization: Bearer <cred>
#   "token"  -> Authorization: token <cred>
#   "bot"    -> Authorization: Bot <cred>
#   "basic"  -> Authorization: Basic <cred>   (cred is pre-encoded base64)
#   ("header", name)     -> send the raw credential in header `name`
#   ("query",  name)     -> send the raw credential as query parameter `name`
#   ("template", tmpl)   -> Authorization: tmpl.format(cred=<cred>), for services
#                           with a non-standard scheme (PagerDuty's
#                           "Token token={cred}", GoodData's "GoodData {cred}")


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
        if kind == "template":
            return {"Authorization": name.format(cred=credential)}, {}
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
    service: RestService,
    endpoint: Endpoint,
    http: HttpClient,
    base_url: str,
    refresher: Callable[[], TokenRefresher | None],
    max_response_bytes: int | None,
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
        query = {k: args[k] for k in endpoint.query if k in args}
        body = {k: args[k] for k in endpoint.body if k in args} or None

        def call(credential: str) -> Any:
            auth_headers, auth_params = _apply_auth(service, credential)
            headers = {**service.headers, **auth_headers}
            params = {**query, **auth_params}
            result = http.request(
                endpoint.method, url, headers=headers, params=params, json=body
            )
            # Bound what reaches the caller's context; overflow is made visible,
            # never silently dropped (see guard_response).
            if max_response_bytes is not None:
                return guard_response(result, max_bytes=max_response_bytes)
            return result

        try:
            return call(credential)
        except ConnectorError as exc:
            # A stale OAuth token surfaces as 401/403; when the operator wired
            # an auto-refresh, renew once and retry so the user never sees it.
            refresh = refresher()
            if refresh is None or exc.status not in (401, 403):
                raise
            refresh(ctx.principal.subject, service.service)
            renewed = ctx.credential()
            if renewed is None or renewed == credential:
                raise  # refresh changed nothing; don't retry into the same wall
            return call(renewed)

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
        max_response_bytes: int | None = None,
    ) -> None:
        if spec.requires_base_url and not base_url:
            raise ValueError(
                f"{spec.service} needs a per-tenant base_url "
                "(e.g. https://your-site.example.com)"
            )
        self.service = spec.service
        self._spec = spec
        # Default the real network client to retry transient failures; an
        # injected client (a fake in tests, or a pre-wrapped one) is used as-is.
        self._http = http or RetryingHttpClient(HttpxClient())
        self._available = available
        self._base_url = base_url or spec.base_url
        self._on_auth_error: TokenRefresher | None = None
        # Opt-in cap on the size of a single response; None means unbounded.
        self._max_response_bytes = max_response_bytes

    def set_auto_refresh(self, refresher: TokenRefresher | None) -> None:
        """Wire a token refresher so a 401/403 renews the user's token and the
        call retries once, transparently. Pass ``OAuthConnect.refresh`` (or use
        ``OAuthConnect.attach_refresh(connector)``). Read at call time, so this
        can be set after the connector is built but before it serves traffic."""
        self._on_auth_error = refresher

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=endpoint.name,
                description=endpoint.description,
                required_scopes=endpoint.scopes,
                handler=_make_handler(
                    self._spec,
                    endpoint,
                    self._http,
                    self._base_url,
                    lambda: self._on_auth_error,
                    self._max_response_bytes,
                ),
                connector=self._spec.service,
                input_schema=endpoint.input_schema(),
            )
            for endpoint in self._spec.endpoints
        ]

    def available(self) -> bool:
        return self._available() if self._available is not None else True
