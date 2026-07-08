"""Does hallpass's tool search actually help -- or would dumb keyword matching
do as well?

A ranker is easy to add and easy to fool yourself about. This benchmark labels
a set of natural-language queries with the catalog tool each should surface,
then scores the shipped LexicalRanker against a naive baseline (count how many
query words appear in the tool's name/description). If the ranker does not beat
the baseline, that is the honest finding and this prints it plainly -- the
point is to measure, not to flatter the code.

Run it:  python evals/tool_search_benchmark.py
"""

from __future__ import annotations

from hallpass import LexicalRanker, catalog, tokenize
from hallpass.gating import ToolSpec

# (query, the tool name it should surface). Labels are against real catalog
# tools; keep them phrased the way a user would ask, not echoing the tool name.
QUERIES: list[tuple[str, str]] = [
    ("list my github repositories", "github_list_my_repos"),
    ("open a new issue on github", "github_create_issue"),
    ("post a message to a slack channel", "slack_post_message"),
    ("who are the people in my slack workspace", "slack_list_users"),
    ("search my notion pages", "notion_search"),
    ("list customers in stripe", "stripe_list_customers"),
    ("recent payment charges", "stripe_list_charges"),
    ("find jira tickets with a query", "jira_search"),
    ("my email templates", "sendgrid_list_templates"),
    ("what is on my calendar", "gcal_list_events"),
    ("show all my calendars", "gcal_list_calendars"),
    ("current on-call incidents", "pagerduty_list_incidents"),
    ("who is on call right now", "pagerduty_list_oncalls"),
    ("list channels in slack", "slack_list_channels"),
    ("outstanding invoices", "stripe_list_invoices"),
    ("list pull requests in a bitbucket repo", "bitbucket_list_pull_requests"),
    ("business locations for square", "square_list_locations"),
    ("support tickets in freshdesk", "freshdesk_list_tickets"),
]


def _all_tools() -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    for name in catalog.names():
        kwargs = {}
        if catalog.requires_base_url(name):
            kwargs["base_url"] = "https://tenant.example.com"
        tools.extend(catalog.load(name, **kwargs).tools())
    return tools


def _naive_rank(query: str, tools: list[ToolSpec]) -> list[ToolSpec]:
    """Baseline: order by how many query tokens appear in name+description.
    A stable sort keeps catalog order for ties, so this is 'keyword overlap,
    no cleverness'."""
    terms = set(tokenize(query))

    def overlap(t: ToolSpec) -> int:
        text = f"{t.name} {t.description}".lower()
        return sum(1 for term in terms if term in text)

    return sorted(tools, key=overlap, reverse=True)


def _rank_of(expected: str, ranked: list[ToolSpec]) -> int:
    for i, spec in enumerate(ranked, start=1):
        if spec.name == expected:
            return i
    return len(ranked) + 1  # not surfaced at all


def _metrics(ranker_name: str, ranked_lists: list[tuple[str, int]]) -> dict:
    ranks = [r for _, r in ranked_lists]
    p_at_1 = sum(1 for r in ranks if r == 1) / len(ranks)
    p_at_3 = sum(1 for r in ranks if r <= 3) / len(ranks)
    mrr = sum(1.0 / r for r in ranks) / len(ranks)
    return {"name": ranker_name, "p@1": p_at_1, "p@3": p_at_3, "mrr": mrr}


def run() -> dict:
    tools = _all_tools()
    ranker = LexicalRanker()
    ranker_ranks, naive_ranks = [], []
    per_query = []
    for query, expected in QUERIES:
        rr = _rank_of(expected, ranker.rank(query, tools))
        nr = _rank_of(expected, _naive_rank(query, tools))
        ranker_ranks.append((query, rr))
        naive_ranks.append((query, nr))
        per_query.append(
            {"query": query, "expected": expected, "ranker": rr, "naive": nr}
        )
    return {
        "n_queries": len(QUERIES),
        "n_tools": len(tools),
        "ranker": _metrics("LexicalRanker", ranker_ranks),
        "naive": _metrics("keyword-overlap", naive_ranks),
        "per_query": per_query,
    }


def main() -> int:
    r = run()
    print(
        f"tool-search benchmark: {r['n_queries']} queries over {r['n_tools']} tools\n"
    )
    print(f"{'query':42s} {'expected':30s} {'ranker':>6s} {'naive':>6s}")
    for row in r["per_query"]:
        print(
            f"{row['query'][:42]:42s} {row['expected'][:30]:30s} "
            f"{row['ranker']:6d} {row['naive']:6d}"
        )
    print()
    for key in ("ranker", "naive"):
        m = r[key]
        print(
            f"{m['name']:16s}  P@1={m['p@1']:.2f}  P@3={m['p@3']:.2f}  MRR={m['mrr']:.3f}"
        )
    verdict = (
        "ranker beats keyword baseline"
        if r["ranker"]["mrr"] > r["naive"]["mrr"]
        else "ranker does NOT beat the keyword baseline (honest negative)"
    )
    print(f"\nverdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
