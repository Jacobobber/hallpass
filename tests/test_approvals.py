"""Separation of duties: an author cannot approve its own work -- enforced at
approval time (ApprovalLedger) and at provisioning time (separation_of_duties
scope check). Each test names the property it pins."""

import pytest

from hallpass import (
    ApprovalError,
    InMemoryApprovalLedger,
    SqliteApprovalLedger,
    separation_of_duties,
)


# -- the pure scope check --------------------------------------------------


def test_sod_flags_co_held_author_and_approve():
    conflict = separation_of_duties({"author:pr-42", "approve:pr-42", "github:read"})
    assert conflict == frozenset({"pr-42"})


def test_sod_no_conflict_when_only_one_side_held():
    assert separation_of_duties({"author:pr-42", "approve:pr-99"}) == frozenset()
    assert separation_of_duties({"github:read", "github:write"}) == frozenset()


def test_sod_custom_prefixes():
    conflict = separation_of_duties(
        {"wrote:x", "signs:x"}, author_prefix="wrote:", approve_prefix="signs:"
    )
    assert conflict == frozenset({"x"})


# -- the approval ledger ---------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    if request.param == "memory":
        yield InMemoryApprovalLedger()
    else:
        led = SqliteApprovalLedger(path=str(tmp_path / "approvals.db"))
        yield led
        led.close()


def test_author_cannot_approve_own_artifact(ledger):
    with pytest.raises(ApprovalError, match="cannot approve artifact 'pr-42'"):
        ledger.record("pr-42", "alice", author="alice")
    assert ledger.approvers("pr-42") == []  # nothing recorded


def test_a_distinct_principal_can_approve(ledger):
    ledger.record("pr-42", "bob", author="alice")
    assert ledger.approvers("pr-42") == ["bob"]
    assert ledger.approved("pr-42") is True


def test_min_approvals_counts_distinct_approvers(ledger):
    ledger.record("pr-42", "bob", author="alice")
    assert ledger.approved("pr-42", min_approvals=2) is False
    ledger.record("pr-42", "carol", author="alice")
    assert ledger.approved("pr-42", min_approvals=2) is True
    assert ledger.approvers("pr-42") == ["bob", "carol"]


def test_re_approval_is_idempotent_per_approver(ledger):
    ledger.record("pr-42", "bob", author="alice")
    ledger.record("pr-42", "bob", author="alice", note="again")
    assert ledger.approvers("pr-42") == ["bob"]  # still one distinct approver
    assert ledger.approved("pr-42", min_approvals=2) is False


def test_approvals_are_per_artifact(ledger):
    ledger.record("pr-1", "bob", author="alice")
    assert ledger.approvers("pr-2") == []
    assert ledger.approved("pr-2") is False


def test_sqlite_approvals_are_durable(tmp_path):
    path = str(tmp_path / "a.db")
    led = SqliteApprovalLedger(path=path)
    led.record("pr-42", "bob", author="alice", note="lgtm")
    led.close()
    reopened = SqliteApprovalLedger(path=path)
    assert reopened.approvers("pr-42") == ["bob"]
    assert reopened.approvals("pr-42")[0].note == "lgtm"
    reopened.close()
