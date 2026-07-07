"""A catalog of prewired connectors, defined as declarations.

Each service below is a ``RestService``: a base URL, an auth style, and a
handful of real endpoints from the service's public REST API. Building one
is a short declaration, which is what lets the catalog grow toward
comprehensive coverage without a per-vendor SDK or bespoke code per service.

Use it:

    from hallpass import catalog
    app.add_connector(catalog.load("github"))          # one connector
    for c in catalog.load_all():                        # every connector
        app.add_connector(c)

Auth: the per-user credential (a PAT or OAuth access token for the service)
must be in the vault under the connector's ``service`` name. hallpass does
not yet run each provider's OAuth flow; getting the token into the vault is
the operator's job for now.
"""

from __future__ import annotations

from collections.abc import Iterable

from .rest import Endpoint, HttpClient, RestConnector, RestService

__all__ = ["names", "load", "load_all", "SERVICES"]


def _ep(
    name: str,
    method: str,
    path: str,
    description: str,
    *,
    scopes: Iterable[str] = (),
    query: tuple[str, ...] = (),
    body: tuple[str, ...] = (),
    required: Iterable[str] = (),
) -> Endpoint:
    return Endpoint(
        name=name,
        description=description,
        method=method,
        path=path,
        scopes=frozenset(scopes),
        query=query,
        body=body,
        required=frozenset(required),
    )


# Real endpoints from each service's public REST API. Tool names are prefixed
# by service so a server hosting many connectors keeps a flat, unambiguous
# namespace. Scopes here are hallpass scopes the caller must hold; map them to
# your own scheme when you register.
SERVICES: dict[str, RestService] = {
    "github": RestService(
        service="github",
        base_url="https://api.github.com",
        auth="bearer",
        headers={"Accept": "application/vnd.github+json"},
        endpoints=(
            _ep(
                "github_list_my_repos",
                "GET",
                "/user/repos",
                "List the caller's repositories.",
                scopes=["github:read"],
                query=("visibility", "per_page"),
            ),
            _ep(
                "github_get_repo",
                "GET",
                "/repos/{owner}/{repo}",
                "Get a repository.",
                scopes=["github:read"],
            ),
            _ep(
                "github_list_issues",
                "GET",
                "/repos/{owner}/{repo}/issues",
                "List issues in a repository.",
                scopes=["github:read"],
                query=("state", "per_page"),
            ),
            _ep(
                "github_create_issue",
                "POST",
                "/repos/{owner}/{repo}/issues",
                "Create an issue.",
                scopes=["github:write"],
                body=("title", "body"),
                required=("title",),
            ),
            _ep(
                "github_search_repos",
                "GET",
                "/search/repositories",
                "Search repositories.",
                scopes=["github:read"],
                query=("q",),
                required=("q",),
            ),
        ),
    ),
    "gitlab": RestService(
        service="gitlab",
        base_url="https://gitlab.com/api/v4",
        auth="bearer",
        endpoints=(
            _ep(
                "gitlab_list_projects",
                "GET",
                "/projects",
                "List projects.",
                scopes=["gitlab:read"],
                query=("membership", "per_page"),
            ),
            _ep(
                "gitlab_get_project",
                "GET",
                "/projects/{id}",
                "Get a project by id.",
                scopes=["gitlab:read"],
            ),
            _ep(
                "gitlab_list_issues",
                "GET",
                "/projects/{id}/issues",
                "List a project's issues.",
                scopes=["gitlab:read"],
                query=("state",),
            ),
        ),
    ),
    "notion": RestService(
        service="notion",
        base_url="https://api.notion.com/v1",
        auth="bearer",
        headers={"Notion-Version": "2022-06-28"},
        endpoints=(
            _ep(
                "notion_search",
                "POST",
                "/search",
                "Search pages and databases.",
                scopes=["notion:read"],
                body=("query",),
            ),
            _ep(
                "notion_get_page",
                "GET",
                "/pages/{page_id}",
                "Get a page.",
                scopes=["notion:read"],
            ),
            _ep(
                "notion_get_database",
                "GET",
                "/databases/{database_id}",
                "Get a database.",
                scopes=["notion:read"],
            ),
            _ep(
                "notion_query_database",
                "POST",
                "/databases/{database_id}/query",
                "Query a database.",
                scopes=["notion:read"],
            ),
        ),
    ),
    "slack": RestService(
        service="slack",
        base_url="https://slack.com/api",
        auth="bearer",
        endpoints=(
            _ep(
                "slack_post_message",
                "POST",
                "/chat.postMessage",
                "Post a message to a channel.",
                scopes=["slack:write"],
                body=("channel", "text"),
                required=("channel", "text"),
            ),
            _ep(
                "slack_list_channels",
                "GET",
                "/conversations.list",
                "List channels.",
                scopes=["slack:read"],
                query=("limit",),
            ),
            _ep(
                "slack_list_users",
                "GET",
                "/users.list",
                "List workspace users.",
                scopes=["slack:read"],
                query=("limit",),
            ),
        ),
    ),
    "google_calendar": RestService(
        service="google_calendar",
        base_url="https://www.googleapis.com/calendar/v3",
        auth="bearer",
        endpoints=(
            _ep(
                "gcal_list_calendars",
                "GET",
                "/users/me/calendarList",
                "List the caller's calendars.",
                scopes=["calendar:read"],
            ),
            _ep(
                "gcal_list_events",
                "GET",
                "/calendars/primary/events",
                "List events on the primary calendar.",
                scopes=["calendar:read"],
                query=("timeMin", "timeMax", "maxResults"),
            ),
            _ep(
                "gcal_get_event",
                "GET",
                "/calendars/primary/events/{event_id}",
                "Get an event.",
                scopes=["calendar:read"],
            ),
        ),
    ),
    "gmail": RestService(
        service="gmail",
        base_url="https://gmail.googleapis.com/gmail/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "gmail_list_messages",
                "GET",
                "/users/me/messages",
                "List message ids.",
                scopes=["gmail:read"],
                query=("q", "maxResults"),
            ),
            _ep(
                "gmail_get_message",
                "GET",
                "/users/me/messages/{message_id}",
                "Get a message.",
                scopes=["gmail:read"],
            ),
            _ep(
                "gmail_list_labels",
                "GET",
                "/users/me/labels",
                "List labels.",
                scopes=["gmail:read"],
            ),
        ),
    ),
    "airtable": RestService(
        service="airtable",
        base_url="https://api.airtable.com/v0",
        auth="bearer",
        endpoints=(
            _ep(
                "airtable_list_records",
                "GET",
                "/{base_id}/{table}",
                "List records in a table.",
                scopes=["airtable:read"],
                query=("pageSize", "view"),
            ),
            _ep(
                "airtable_get_record",
                "GET",
                "/{base_id}/{table}/{record_id}",
                "Get a record.",
                scopes=["airtable:read"],
            ),
        ),
    ),
    "hubspot": RestService(
        service="hubspot",
        base_url="https://api.hubapi.com",
        auth="bearer",
        endpoints=(
            _ep(
                "hubspot_list_contacts",
                "GET",
                "/crm/v3/objects/contacts",
                "List contacts.",
                scopes=["hubspot:read"],
                query=("limit",),
            ),
            _ep(
                "hubspot_get_contact",
                "GET",
                "/crm/v3/objects/contacts/{contact_id}",
                "Get a contact.",
                scopes=["hubspot:read"],
            ),
            _ep(
                "hubspot_list_deals",
                "GET",
                "/crm/v3/objects/deals",
                "List deals.",
                scopes=["hubspot:read"],
                query=("limit",),
            ),
        ),
    ),
    "discord": RestService(
        service="discord",
        base_url="https://discord.com/api/v10",
        auth="bot",
        endpoints=(
            _ep(
                "discord_list_my_guilds",
                "GET",
                "/users/@me/guilds",
                "List the bot's guilds.",
                scopes=["discord:read"],
            ),
            _ep(
                "discord_get_channel",
                "GET",
                "/channels/{channel_id}",
                "Get a channel.",
                scopes=["discord:read"],
            ),
            _ep(
                "discord_create_message",
                "POST",
                "/channels/{channel_id}/messages",
                "Send a message.",
                scopes=["discord:write"],
                body=("content",),
                required=("content",),
            ),
        ),
    ),
    "sentry": RestService(
        service="sentry",
        base_url="https://sentry.io/api/0",
        auth="bearer",
        endpoints=(
            _ep(
                "sentry_list_projects",
                "GET",
                "/projects/",
                "List projects.",
                scopes=["sentry:read"],
            ),
            _ep(
                "sentry_list_issues",
                "GET",
                "/projects/{organization_slug}/{project_slug}/issues/",
                "List a project's issues.",
                scopes=["sentry:read"],
                query=("query",),
            ),
        ),
    ),
    "asana": RestService(
        service="asana",
        base_url="https://app.asana.com/api/1.0",
        auth="bearer",
        endpoints=(
            _ep(
                "asana_list_workspaces",
                "GET",
                "/workspaces",
                "List workspaces.",
                scopes=["asana:read"],
            ),
            _ep(
                "asana_list_tasks",
                "GET",
                "/tasks",
                "List tasks.",
                scopes=["asana:read"],
                query=("project", "assignee"),
            ),
            _ep(
                "asana_get_task",
                "GET",
                "/tasks/{task_gid}",
                "Get a task.",
                scopes=["asana:read"],
            ),
        ),
    ),
    "linear": RestService(
        service="linear",
        base_url="https://api.linear.app",
        auth="bearer",
        endpoints=(
            # Linear is GraphQL: a single POST /graphql with a query body.
            _ep(
                "linear_graphql",
                "POST",
                "/graphql",
                "Run a Linear GraphQL query.",
                scopes=["linear:read"],
                body=("query", "variables"),
                required=("query",),
            ),
        ),
    ),
    "figma": RestService(
        service="figma",
        base_url="https://api.figma.com/v1",
        auth=("X-Figma-Token",),
        endpoints=(
            _ep(
                "figma_get_me",
                "GET",
                "/me",
                "Get the authenticated user.",
                scopes=["figma:read"],
            ),
            _ep(
                "figma_get_file",
                "GET",
                "/files/{file_key}",
                "Get a file.",
                scopes=["figma:read"],
            ),
        ),
    ),
    "vercel": RestService(
        service="vercel",
        base_url="https://api.vercel.com",
        auth="bearer",
        endpoints=(
            _ep(
                "vercel_list_projects",
                "GET",
                "/v9/projects",
                "List projects.",
                scopes=["vercel:read"],
                query=("limit",),
            ),
            _ep(
                "vercel_list_deployments",
                "GET",
                "/v6/deployments",
                "List deployments.",
                scopes=["vercel:read"],
                query=("limit",),
            ),
        ),
    ),
    "sendgrid": RestService(
        service="sendgrid",
        base_url="https://api.sendgrid.com/v3",
        auth="bearer",
        endpoints=(
            _ep(
                "sendgrid_list_templates",
                "GET",
                "/templates",
                "List email templates.",
                scopes=["sendgrid:read"],
                query=("generations",),
            ),
        ),
    ),
    "intercom": RestService(
        service="intercom",
        base_url="https://api.intercom.io",
        auth="bearer",
        endpoints=(
            _ep(
                "intercom_list_contacts",
                "GET",
                "/contacts",
                "List contacts.",
                scopes=["intercom:read"],
            ),
            _ep(
                "intercom_get_contact",
                "GET",
                "/contacts/{contact_id}",
                "Get a contact.",
                scopes=["intercom:read"],
            ),
        ),
    ),
    "calendly": RestService(
        service="calendly",
        base_url="https://api.calendly.com",
        auth="bearer",
        endpoints=(
            _ep(
                "calendly_get_me",
                "GET",
                "/users/me",
                "Get the current user.",
                scopes=["calendly:read"],
            ),
            _ep(
                "calendly_list_events",
                "GET",
                "/scheduled_events",
                "List scheduled events.",
                scopes=["calendly:read"],
                query=("user", "count"),
            ),
        ),
    ),
    "cloudflare": RestService(
        service="cloudflare",
        base_url="https://api.cloudflare.com/client/v4",
        auth="bearer",
        endpoints=(
            _ep(
                "cloudflare_list_zones",
                "GET",
                "/zones",
                "List zones.",
                scopes=["cloudflare:read"],
                query=("name", "per_page"),
            ),
        ),
    ),
    "digitalocean": RestService(
        service="digitalocean",
        base_url="https://api.digitalocean.com/v2",
        auth="bearer",
        endpoints=(
            _ep(
                "do_list_droplets",
                "GET",
                "/droplets",
                "List droplets.",
                scopes=["digitalocean:read"],
                query=("per_page",),
            ),
            _ep(
                "do_list_projects",
                "GET",
                "/projects",
                "List projects.",
                scopes=["digitalocean:read"],
            ),
        ),
    ),
    "openai": RestService(
        service="openai",
        base_url="https://api.openai.com/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "openai_list_models",
                "GET",
                "/models",
                "List available models.",
                scopes=["openai:read"],
            ),
        ),
    ),
}


def names() -> list[str]:
    """Every connector name in the catalog."""
    return sorted(SERVICES)


def load(name: str, *, http: HttpClient | None = None) -> RestConnector:
    """Build one prewired connector by name. Pass ``http`` to inject a
    client (tests do this); the default uses httpx (the ``connectors``
    extra)."""
    if name not in SERVICES:
        raise KeyError(f"no connector named {name!r}; see catalog.names()")
    return RestConnector(SERVICES[name], http=http)


def load_all(*, http: HttpClient | None = None) -> list[RestConnector]:
    """Build every connector in the catalog."""
    return [RestConnector(SERVICES[n], http=http) for n in names()]
