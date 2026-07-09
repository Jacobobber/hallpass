# Contributing

Issues and questions are welcome. The most useful contribution is a new
connector, and it is meant to be easy.

## Setup (clone to green in two commands)

```bash
git clone https://github.com/Jacobobber/hallpass && cd hallpass
uv run --group dev pytest -q          # builds the env and runs the suite
```

`uv` is the only prerequisite. If you prefer a Makefile: `make test`, `make lint`, `make catalog`, `make demo`.

## Add a connector to the catalog

A connector is a declaration, not code. Add a `RestService` entry to
`src/hallpass/catalog.py`:

```python
"acme": RestService(
    service="acme",                       # the vault credential slot
    base_url="https://api.acme.com/v1",
    auth="bearer",                        # bearer | token | bot | basic | ("header", n) | ("query", n)
    endpoints=(
        _ep("acme_list_widgets", "GET", "/widgets", "List widgets.",
            scopes=["acme:read"], query=("limit",)),
        _ep("acme_create_widget", "POST", "/widgets", "Create a widget.",
            scopes=["acme:write"], body=("name",), required=("name",)),
    ),
),
```

- Path parameters (`/widgets/{id}`) become required tool arguments automatically.
- `query` args go on the query string; `body` args go in the JSON body; `required` marks non-path args that must be supplied.
- For a per-tenant service (host differs per customer, e.g. Jira), set `base_url=""` and `requires_base_url=True`; callers pass `catalog.load("acme", base_url="https://their-host")`.

Then regenerate the catalog doc and run the checks:

```bash
python scripts/gen_catalog.py          # updates docs/CATALOG.md
uv run --group dev pytest -q
uv run --with ruff ruff check . && uv run --with ruff ruff format .
uv run --group dev --with mypy mypy --strict src
```

CI runs all of these plus `python scripts/gen_catalog.py --check`, so a stale
catalog doc fails the build.

The whole system, layer by layer, is mapped in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —
read it first if you are changing anything beyond a connector.

## Bar for changes

Same as the rest of hallpass: deny by default where it is a boundary, a test
for anything with a security property, `mypy --strict`, and no secret in any
log or error. This bar applies to the coordination layers (channels,
orchestration, routing, queue, spawning) exactly as it does to the auth core —
each isolation property ships as a test that names the failure it prevents, or
the boundary quietly drifts. A connector needs at least one real, correct
endpoint; prefer a few correct ones over many guessed. Custom-typed request
bodies or non-REST services (GraphQL, form-encoded) may need a hand-written
connector rather than a catalog declaration; see `docs/IDEAS.md`.

## Writing a connector as code

If declarations do not fit, a connector is any object with `service`, a
`tools()` returning `ToolSpec`s, and an optional `available()`. The `ToolKit`
decorator (see the README quick start) is the easy path for that.

## Security reports

See [SECURITY.md](SECURITY.md).
