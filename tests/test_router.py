"""Auth-native routing: a task is routed to a worker whose harness (its granted
scopes) covers the scopes the task needs, so work never lands on an agent that
isn't authorized to do it. What matters: only capable workers are candidates,
routing round-robins across them, and an unroutable task is visibly None (not a
silent misroute)."""

from hallpass import Router


def _router():
    r = Router()
    r.register("reviewer", {"github:read", "github:write"})
    r.register("reader", {"github:read"})
    r.register("everything", {"github:read", "github:write", "slack:write"})
    return r


def test_candidates_are_only_capable_workers():
    r = _router()
    assert r.candidates({"github:read"}) == ["everything", "reader", "reviewer"]
    assert r.candidates({"github:write"}) == ["everything", "reviewer"]
    assert r.candidates({"slack:write"}) == ["everything"]
    assert r.candidates({"pagerduty:read"}) == []  # nobody has it


def test_route_picks_a_capable_worker():
    r = _router()
    picked = r.route({"github:write"})
    assert picked in {"everything", "reviewer"}  # both can, neither "reader"


def test_route_round_robins_across_eligible():
    r = _router()
    # three workers can do github:read; consecutive routes should rotate
    picks = [r.route({"github:read"}) for _ in range(3)]
    assert set(picks) == {"everything", "reader", "reviewer"}  # spread, not stuck


def test_unroutable_task_returns_none():
    r = _router()
    assert r.route({"pagerduty:read"}) is None  # no capable worker -> visible None


def test_route_needs_all_required_scopes():
    r = Router()
    r.register("partial", {"a:read"})  # has one of the two
    assert r.route({"a:read", "b:read"}) is None
    r.register("full", {"a:read", "b:read"})
    assert r.route({"a:read", "b:read"}) == "full"


def test_empty_requirement_matches_any_worker():
    r = _router()
    assert r.route(set()) is not None  # a task needing nothing can go anywhere
