"""A single connector call can return a firehose that crowds out the agent's
whole context. guard_response bounds it -- but the property that matters is
that overflow is never silent: an oversized result is replaced by an explicit
envelope the model can see and act on, not quietly truncated into a partial
result it mistakes for the whole.

The failures these prevent: a 50k-row list silently eating the context window,
and (worse) a truncated result that looks complete so the agent reasons over a
fraction believing it saw everything."""

import json

from hallpass import TRUNCATED_KEY, guard_response
from hallpass import catalog, dev_app


def test_small_response_passes_through_unchanged():
    value = {"items": [1, 2, 3], "next": None}
    assert guard_response(value, max_bytes=10_000) is value


def test_oversized_response_becomes_a_visible_envelope():
    big = {"rows": ["x" * 100 for _ in range(500)]}  # ~50KB serialized
    out = guard_response(big, max_bytes=2_000)
    assert out is not big
    assert out[TRUNCATED_KEY] is True
    assert out["bytes"] > out["max_bytes"] == 2_000
    assert "narrow" in out["reason"].lower() or "less" in out["reason"].lower()
    # a preview is present and the whole envelope stays within budget
    assert out["preview"]
    assert len(json.dumps(out).encode("utf-8")) <= 2_000


def test_preview_is_utf8_safe_at_the_cut():
    # a multibyte char straddling the preview byte budget must not raise; a
    # short ASCII prefix then a long 2-byte run guarantees the cut lands mid-char
    value = "A" * 10 + "é" * 2000
    out = guard_response(value, max_bytes=1_000)  # >budget, cut inside the é run
    assert out[TRUNCATED_KEY] is True
    assert isinstance(out["preview"], str)  # decoded with errors ignored, no junk
    assert chr(0xFFFD) not in out["preview"]  # no replacement char left behind


def test_non_json_serializable_value_is_still_bounded():
    class Weird:
        def __repr__(self):
            return "W" * 5_000

    out = guard_response([Weird()], max_bytes=1_000)
    assert out[TRUNCATED_KEY] is True  # fell back to str(), still guarded


def test_exact_limit_is_not_truncated():
    value = "a" * 100
    size = len(json.dumps(value).encode("utf-8"))  # 102 incl. quotes
    assert guard_response(value, max_bytes=size) == value
    assert guard_response(value, max_bytes=size - 1) != value


# -- connector integration -------------------------------------------------


def test_connector_guards_an_oversized_response():
    class BigHttp:
        def request(self, method, url, *, headers, params, json):
            return [{"id": i, "blob": "z" * 200} for i in range(1000)]

    gh = catalog.load("github", http=BigHttp(), max_response_bytes=4_000)
    app, token = dev_app(connectors=[gh])
    app._vault.store("alice", "github", "ghp_x")
    out = app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert out[TRUNCATED_KEY] is True
    assert out["bytes"] > 4_000
    app.close()


def test_connector_without_cap_returns_full_response():
    class BigHttp:
        def request(self, method, url, *, headers, params, json):
            return [{"id": i} for i in range(1000)]

    gh = catalog.load("github", http=BigHttp())  # no cap
    app, token = dev_app(connectors=[gh])
    app._vault.store("alice", "github", "ghp_x")
    out = app.call_tool(token("alice", ["github:read"]), "github_list_my_repos", {})
    assert isinstance(out, list) and len(out) == 1000
    app.close()
