"""Regression guard over the tool-search benchmark (evals/tool_search_benchmark).

The benchmark is the honest measurement; this pins the result so a future
ranking change that quietly makes search worse than dumb keyword matching -- or
worse than it is today -- fails CI instead of shipping."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.tool_search_benchmark import run  # noqa: E402


def test_ranker_beats_keyword_baseline():
    r = run()
    # the headline claim: the lexical ranker adds value over naive overlap
    assert r["ranker"]["mrr"] > r["naive"]["mrr"], r
    # and it surfaces the right tool in the top 3 for essentially every query
    assert r["ranker"]["p@3"] >= 0.9, r
    assert r["ranker"]["p@1"] >= 0.8, r


def test_every_label_resolves_to_a_real_tool():
    # a mislabeled query would silently score as "never surfaced"; catch it
    r = run()
    assert all(row["ranker"] <= r["n_tools"] for row in r["per_query"]), r
