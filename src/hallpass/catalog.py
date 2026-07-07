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
lives in the vault under the connector's ``service`` name. For services with
a known OAuth flow, ``oauth_provider(name, ...)`` builds an ``OAuthProvider``
so ``OAuthConnect`` can put that token there automatically (see OAUTH below).
"""

from __future__ import annotations

from collections.abc import Iterable

from .oauth import OAuthProvider
from .rest import Endpoint, HttpClient, RestConnector, RestService

__all__ = [
    "names",
    "load",
    "load_all",
    "requires_base_url",
    "oauth_provider",
    "oauth_services",
    "SERVICES",
    "OAUTH",
]


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
        auth=("header", "X-Figma-Token"),
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
    "anthropic": RestService(
        service="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth=("header", "x-api-key"),
        headers={"anthropic-version": "2023-06-01"},
        endpoints=(
            _ep(
                "anthropic_list_models",
                "GET",
                "/models",
                "List available models.",
                scopes=["anthropic:read"],
            ),
        ),
    ),
    "mistral": RestService(
        service="mistral",
        base_url="https://api.mistral.ai/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "mistral_list_models",
                "GET",
                "/models",
                "List available models.",
                scopes=["mistral:read"],
            ),
        ),
    ),
    "groq": RestService(
        service="groq",
        base_url="https://api.groq.com/openai/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "groq_list_models",
                "GET",
                "/models",
                "List available models.",
                scopes=["groq:read"],
            ),
        ),
    ),
    "huggingface": RestService(
        service="huggingface",
        base_url="https://huggingface.co/api",
        auth="bearer",
        endpoints=(
            _ep(
                "hf_whoami",
                "GET",
                "/whoami-v2",
                "Get the authenticated user.",
                scopes=["huggingface:read"],
            ),
            _ep(
                "hf_list_models",
                "GET",
                "/models",
                "List models.",
                scopes=["huggingface:read"],
                query=("search", "limit"),
            ),
        ),
    ),
    "google_drive": RestService(
        service="google_drive",
        base_url="https://www.googleapis.com/drive/v3",
        auth="bearer",
        endpoints=(
            _ep(
                "gdrive_list_files",
                "GET",
                "/files",
                "List files.",
                scopes=["drive:read"],
                query=("q", "pageSize"),
            ),
            _ep(
                "gdrive_get_file",
                "GET",
                "/files/{file_id}",
                "Get a file's metadata.",
                scopes=["drive:read"],
            ),
        ),
    ),
    "google_sheets": RestService(
        service="google_sheets",
        base_url="https://sheets.googleapis.com/v4",
        auth="bearer",
        endpoints=(
            _ep(
                "gsheets_get_spreadsheet",
                "GET",
                "/spreadsheets/{spreadsheet_id}",
                "Get a spreadsheet.",
                scopes=["sheets:read"],
            ),
            _ep(
                "gsheets_get_values",
                "GET",
                "/spreadsheets/{spreadsheet_id}/values/{range}",
                "Get cell values in a range.",
                scopes=["sheets:read"],
            ),
        ),
    ),
    "google_docs": RestService(
        service="google_docs",
        base_url="https://docs.googleapis.com/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "gdocs_get_document",
                "GET",
                "/documents/{document_id}",
                "Get a document.",
                scopes=["docs:read"],
            ),
        ),
    ),
    "microsoft_graph": RestService(
        service="microsoft_graph",
        base_url="https://graph.microsoft.com/v1.0",
        auth="bearer",
        endpoints=(
            _ep(
                "msgraph_get_me",
                "GET",
                "/me",
                "Get the signed-in user.",
                scopes=["msgraph:read"],
            ),
            _ep(
                "msgraph_list_messages",
                "GET",
                "/me/messages",
                "List the user's mail.",
                scopes=["msgraph:read"],
                query=("$top",),
            ),
            _ep(
                "msgraph_list_events",
                "GET",
                "/me/events",
                "List calendar events.",
                scopes=["msgraph:read"],
                query=("$top",),
            ),
            _ep(
                "msgraph_list_drive_root",
                "GET",
                "/me/drive/root/children",
                "List files in the OneDrive root.",
                scopes=["msgraph:read"],
            ),
        ),
    ),
    "box": RestService(
        service="box",
        base_url="https://api.box.com/2.0",
        auth="bearer",
        endpoints=(
            _ep(
                "box_get_me",
                "GET",
                "/users/me",
                "Get the current user.",
                scopes=["box:read"],
            ),
            _ep(
                "box_list_folder_items",
                "GET",
                "/folders/{folder_id}/items",
                "List items in a folder.",
                scopes=["box:read"],
            ),
        ),
    ),
    "todoist": RestService(
        service="todoist",
        base_url="https://api.todoist.com/rest/v2",
        auth="bearer",
        endpoints=(
            _ep(
                "todoist_get_tasks",
                "GET",
                "/tasks",
                "List active tasks.",
                scopes=["todoist:read"],
                query=("project_id",),
            ),
            _ep(
                "todoist_get_projects",
                "GET",
                "/projects",
                "List projects.",
                scopes=["todoist:read"],
            ),
            _ep(
                "todoist_create_task",
                "POST",
                "/tasks",
                "Create a task.",
                scopes=["todoist:write"],
                body=("content", "project_id"),
                required=("content",),
            ),
        ),
    ),
    "zoom": RestService(
        service="zoom",
        base_url="https://api.zoom.us/v2",
        auth="bearer",
        endpoints=(
            _ep(
                "zoom_get_me",
                "GET",
                "/users/me",
                "Get the current user.",
                scopes=["zoom:read"],
            ),
            _ep(
                "zoom_list_meetings",
                "GET",
                "/users/me/meetings",
                "List the user's meetings.",
                scopes=["zoom:read"],
                query=("type",),
            ),
        ),
    ),
    "spotify": RestService(
        service="spotify",
        base_url="https://api.spotify.com/v1",
        auth="bearer",
        endpoints=(
            _ep(
                "spotify_get_me",
                "GET",
                "/me",
                "Get the current user's profile.",
                scopes=["spotify:read"],
            ),
            _ep(
                "spotify_get_playlists",
                "GET",
                "/me/playlists",
                "List the user's playlists.",
                scopes=["spotify:read"],
                query=("limit",),
            ),
            _ep(
                "spotify_search",
                "GET",
                "/search",
                "Search the catalog.",
                scopes=["spotify:read"],
                query=("q", "type"),
                required=("q", "type"),
            ),
        ),
    ),
    "monday": RestService(
        service="monday",
        base_url="https://api.monday.com",
        auth="bearer",
        endpoints=(
            # monday.com is GraphQL: a single POST /v2 with a query body.
            _ep(
                "monday_graphql",
                "POST",
                "/v2",
                "Run a monday.com GraphQL query.",
                scopes=["monday:read"],
                body=("query", "variables"),
                required=("query",),
            ),
        ),
    ),
    "clickup": RestService(
        service="clickup",
        base_url="https://api.clickup.com/api/v2",
        auth=(
            "header",
            "Authorization",
        ),  # ClickUp personal token, raw in Authorization
        endpoints=(
            _ep(
                "clickup_list_teams",
                "GET",
                "/team",
                "List workspaces (teams).",
                scopes=["clickup:read"],
            ),
            _ep(
                "clickup_get_task",
                "GET",
                "/task/{task_id}",
                "Get a task.",
                scopes=["clickup:read"],
            ),
        ),
    ),
    "pipedrive": RestService(
        service="pipedrive",
        base_url="https://api.pipedrive.com/v1",
        auth=("query", "api_token"),  # Pipedrive passes the token as a query param
        endpoints=(
            _ep(
                "pipedrive_list_deals",
                "GET",
                "/deals",
                "List deals.",
                scopes=["pipedrive:read"],
                query=("status",),
            ),
            _ep(
                "pipedrive_list_persons",
                "GET",
                "/persons",
                "List persons.",
                scopes=["pipedrive:read"],
            ),
        ),
    ),
    "postmark": RestService(
        service="postmark",
        base_url="https://api.postmarkapp.com",
        auth=("header", "X-Postmark-Server-Token"),
        headers={"Accept": "application/json"},
        endpoints=(
            _ep(
                "postmark_get_server",
                "GET",
                "/server",
                "Get the server configuration.",
                scopes=["postmark:read"],
            ),
        ),
    ),
    "jira": RestService(
        service="jira",
        base_url="",  # per-tenant, e.g. https://your-site.atlassian.net
        auth="basic",  # base64("email:api_token")
        requires_base_url=True,
        endpoints=(
            _ep(
                "jira_get_myself",
                "GET",
                "/rest/api/3/myself",
                "Get the current user.",
                scopes=["jira:read"],
            ),
            _ep(
                "jira_search",
                "GET",
                "/rest/api/3/search",
                "Search issues with JQL.",
                scopes=["jira:read"],
                query=("jql", "maxResults"),
                required=("jql",),
            ),
            _ep(
                "jira_get_issue",
                "GET",
                "/rest/api/3/issue/{issue_key}",
                "Get an issue.",
                scopes=["jira:read"],
            ),
        ),
    ),
    "confluence": RestService(
        service="confluence",
        base_url="",  # per-tenant, e.g. https://your-site.atlassian.net
        auth="basic",
        requires_base_url=True,
        endpoints=(
            _ep(
                "confluence_list_spaces",
                "GET",
                "/wiki/rest/api/space",
                "List spaces.",
                scopes=["confluence:read"],
                query=("limit",),
            ),
            _ep(
                "confluence_get_content",
                "GET",
                "/wiki/rest/api/content/{content_id}",
                "Get a content item.",
                scopes=["confluence:read"],
            ),
        ),
    ),
    "zendesk": RestService(
        service="zendesk",
        base_url="",  # per-tenant, e.g. https://your-subdomain.zendesk.com
        auth="basic",  # base64("email/token:api_token")
        requires_base_url=True,
        endpoints=(
            _ep(
                "zendesk_list_tickets",
                "GET",
                "/api/v2/tickets.json",
                "List tickets.",
                scopes=["zendesk:read"],
            ),
            _ep(
                "zendesk_get_ticket",
                "GET",
                "/api/v2/tickets/{ticket_id}.json",
                "Get a ticket.",
                scopes=["zendesk:read"],
            ),
        ),
    ),
    "shopify": RestService(
        service="shopify",
        base_url="",  # per-shop, e.g. https://your-shop.myshopify.com
        auth=("header", "X-Shopify-Access-Token"),
        requires_base_url=True,
        endpoints=(
            _ep(
                "shopify_list_products",
                "GET",
                "/admin/api/2024-01/products.json",
                "List products.",
                scopes=["shopify:read"],
                query=("limit",),
            ),
            _ep(
                "shopify_list_orders",
                "GET",
                "/admin/api/2024-01/orders.json",
                "List orders.",
                scopes=["shopify:read"],
                query=("status",),
            ),
        ),
    ),
    "salesforce": RestService(
        service="salesforce",
        base_url="",  # per-instance, e.g. https://your-instance.my.salesforce.com
        auth="bearer",
        requires_base_url=True,
        endpoints=(
            _ep(
                "salesforce_query",
                "GET",
                "/services/data/v59.0/query",
                "Run a SOQL query.",
                scopes=["salesforce:read"],
                query=("q",),
                required=("q",),
            ),
        ),
    ),
    "pagerduty": RestService(
        service="pagerduty",
        base_url="https://api.pagerduty.com",
        # Non-standard scheme -- the templated auth style exists for this.
        auth=("template", "Token token={cred}"),
        headers={"Accept": "application/vnd.pagerduty+json;version=2"},
        endpoints=(
            _ep(
                "pagerduty_list_incidents",
                "GET",
                "/incidents",
                "List incidents.",
                scopes=["pagerduty:read"],
                query=("statuses[]", "since", "until", "limit"),
            ),
            _ep(
                "pagerduty_get_incident",
                "GET",
                "/incidents/{id}",
                "Get an incident by id.",
                scopes=["pagerduty:read"],
            ),
            _ep(
                "pagerduty_list_services",
                "GET",
                "/services",
                "List services.",
                scopes=["pagerduty:read"],
                query=("limit",),
            ),
            _ep(
                "pagerduty_list_oncalls",
                "GET",
                "/oncalls",
                "List who is currently on call.",
                scopes=["pagerduty:read"],
                query=("since", "until"),
            ),
        ),
    ),
    "stripe": RestService(
        service="stripe",
        base_url="https://api.stripe.com/v1",
        auth="bearer",
        # Read endpoints only for now: Stripe writes are form-encoded, which
        # the framework does not yet send (see docs/IDEAS.md).
        endpoints=(
            _ep(
                "stripe_list_charges",
                "GET",
                "/charges",
                "List charges.",
                scopes=["stripe:read"],
                query=("limit", "customer"),
            ),
            _ep(
                "stripe_list_customers",
                "GET",
                "/customers",
                "List customers.",
                scopes=["stripe:read"],
                query=("limit", "email"),
            ),
            _ep(
                "stripe_list_invoices",
                "GET",
                "/invoices",
                "List invoices.",
                scopes=["stripe:read"],
                query=("limit", "customer", "status"),
            ),
            _ep(
                "stripe_get_balance",
                "GET",
                "/balance",
                "Retrieve the account balance.",
                scopes=["stripe:read"],
            ),
        ),
    ),
    "bitbucket": RestService(
        service="bitbucket",
        base_url="https://api.bitbucket.org/2.0",
        auth="bearer",
        endpoints=(
            _ep(
                "bitbucket_list_workspaces",
                "GET",
                "/workspaces",
                "List the caller's workspaces.",
                scopes=["bitbucket:read"],
            ),
            _ep(
                "bitbucket_list_repos",
                "GET",
                "/repositories/{workspace}",
                "List repositories in a workspace.",
                scopes=["bitbucket:read"],
                query=("q", "sort"),
            ),
            _ep(
                "bitbucket_get_repo",
                "GET",
                "/repositories/{workspace}/{repo_slug}",
                "Get a repository.",
                scopes=["bitbucket:read"],
            ),
            _ep(
                "bitbucket_list_pull_requests",
                "GET",
                "/repositories/{workspace}/{repo_slug}/pullrequests",
                "List pull requests in a repository.",
                scopes=["bitbucket:read"],
                query=("state",),
            ),
        ),
    ),
    "square": RestService(
        service="square",
        base_url="https://connect.squareup.com/v2",
        auth="bearer",
        headers={"Square-Version": "2024-01-18"},
        endpoints=(
            _ep(
                "square_list_locations",
                "GET",
                "/locations",
                "List business locations.",
                scopes=["square:read"],
            ),
            _ep(
                "square_list_payments",
                "GET",
                "/payments",
                "List payments.",
                scopes=["square:read"],
                query=("begin_time", "end_time", "location_id"),
            ),
            _ep(
                "square_list_customers",
                "GET",
                "/customers",
                "List customers.",
                scopes=["square:read"],
                query=("cursor",),
            ),
        ),
    ),
    "freshdesk": RestService(
        service="freshdesk",
        base_url="",  # per-tenant, e.g. https://your-domain.freshdesk.com/api/v2
        # Freshdesk uses HTTP Basic with the API key as the username; supply
        # the credential pre-encoded as base64("<api_key>:X").
        auth="basic",
        requires_base_url=True,
        endpoints=(
            _ep(
                "freshdesk_list_tickets",
                "GET",
                "/tickets",
                "List tickets.",
                scopes=["freshdesk:read"],
                query=("updated_since", "per_page"),
            ),
            _ep(
                "freshdesk_get_ticket",
                "GET",
                "/tickets/{id}",
                "Get a ticket by id.",
                scopes=["freshdesk:read"],
            ),
            _ep(
                "freshdesk_list_contacts",
                "GET",
                "/contacts",
                "List contacts.",
                scopes=["freshdesk:read"],
                query=("email", "per_page"),
            ),
        ),
    ),
}


def names() -> list[str]:
    """Every connector name in the catalog."""
    return sorted(SERVICES)


def requires_base_url(name: str) -> bool:
    """Whether a connector needs a per-tenant base URL supplied at load
    (Jira, Confluence, Zendesk, Shopify, Salesforce)."""
    return SERVICES[name].requires_base_url


def load(
    name: str, *, http: HttpClient | None = None, base_url: str | None = None
) -> RestConnector:
    """Build one prewired connector by name. Pass ``http`` to inject a client
    (tests do this); the default uses httpx (the ``connectors`` extra). For a
    per-tenant service pass ``base_url`` with the tenant host, e.g.
    ``load("jira", base_url="https://your-site.atlassian.net")``."""
    if name not in SERVICES:
        raise KeyError(f"no connector named {name!r}; see catalog.names()")
    return RestConnector(SERVICES[name], http=http, base_url=base_url)


def load_all(*, http: HttpClient | None = None) -> list[RestConnector]:
    """Build every connector that does not need per-tenant configuration.
    Per-tenant services (see ``requires_base_url``) are skipped because they
    need a base URL; load those individually with ``load(name, base_url=...)``."""
    return [
        RestConnector(SERVICES[n], http=http)
        for n in names()
        if not SERVICES[n].requires_base_url
    ]


# Known OAuth endpoints for services with a standard authorization-code flow:
# service -> (authorize_url, token_url, default scopes). The operator supplies
# the client id/secret and redirect URI via oauth_provider(). Google services
# (drive, gmail, calendar, docs, sheets) share one OAuth flow; request the
# per-API scopes you need.
OAUTH: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "github": (
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        ("repo", "read:user"),
    ),
    "gitlab": (
        "https://gitlab.com/oauth/authorize",
        "https://gitlab.com/oauth/token",
        ("read_api",),
    ),
    "slack": (
        "https://slack.com/oauth/v2/authorize",
        "https://slack.com/api/oauth.v2.access",
        ("channels:read", "chat:write"),
    ),
    "notion": (
        "https://api.notion.com/v1/oauth/authorize",
        "https://api.notion.com/v1/oauth/token",
        (),
    ),
    "discord": (
        "https://discord.com/oauth2/authorize",
        "https://discord.com/api/oauth2/token",
        ("identify", "guilds"),
    ),
    "spotify": (
        "https://accounts.spotify.com/authorize",
        "https://accounts.spotify.com/api/token",
        ("user-read-private", "playlist-read-private"),
    ),
    "zoom": ("https://zoom.us/oauth/authorize", "https://zoom.us/oauth/token", ()),
    "hubspot": (
        "https://app.hubspot.com/oauth/authorize",
        "https://api.hubapi.com/oauth/v1/token",
        ("crm.objects.contacts.read",),
    ),
    "linear": (
        "https://linear.app/oauth/authorize",
        "https://api.linear.app/oauth/token",
        ("read",),
    ),
    "bitbucket": (
        "https://bitbucket.org/site/oauth2/authorize",
        "https://bitbucket.org/site/oauth2/access_token",
        ("repository", "account"),
    ),
    "square": (
        "https://connect.squareup.com/oauth2/authorize",
        "https://connect.squareup.com/oauth2/token",
        ("MERCHANT_PROFILE_READ", "PAYMENTS_READ", "CUSTOMERS_READ"),
    ),
    "asana": (
        "https://app.asana.com/-/oauth_authorize",
        "https://app.asana.com/-/oauth_token",
        ("default",),
    ),
    "box": (
        "https://account.box.com/api/oauth2/authorize",
        "https://api.box.com/oauth2/token",
        (),
    ),
    "figma": (
        "https://www.figma.com/oauth",
        "https://api.figma.com/v1/oauth/token",
        ("file_read",),
    ),
    "calendly": (
        "https://auth.calendly.com/oauth/authorize",
        "https://auth.calendly.com/oauth/token",
        (),
    ),
    "intercom": (
        "https://app.intercom.com/oauth",
        "https://api.intercom.io/auth/eagle/token",
        (),
    ),
    "microsoft_graph": (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ("User.Read", "offline_access"),
    ),
    "google_drive": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/drive.readonly",),
    ),
    "gmail": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/gmail.readonly",),
    ),
    "google_calendar": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/calendar.readonly",),
    ),
    "google_docs": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/documents.readonly",),
    ),
    "google_sheets": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        ("https://www.googleapis.com/auth/spreadsheets.readonly",),
    ),
}


def oauth_services() -> list[str]:
    """Catalog services with a known OAuth flow."""
    return sorted(OAUTH)


def oauth_provider(
    name: str,
    *,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
    scopes: Iterable[str] | None = None,
    use_pkce: bool = True,
) -> OAuthProvider:
    """Build an OAuthProvider for a known service from operator credentials.
    Defaults to the service's usual scopes; override with ``scopes``."""
    if name not in OAUTH:
        raise KeyError(
            f"no known OAuth flow for {name!r}; see catalog.oauth_services()"
        )
    authorize_url, token_url, default_scopes = OAUTH[name]
    return OAuthProvider(
        authorize_url=authorize_url,
        token_url=token_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
        client_secret=client_secret,
        scopes=tuple(scopes) if scopes is not None else default_scopes,
        use_pkce=use_pkce,
    )
