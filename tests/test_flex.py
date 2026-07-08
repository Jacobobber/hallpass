"""FLEX is the token-efficient A2A message language. What matters: it
round-trips (parse(encode(m)) == m) so structure is never lost; it is tolerant
of hand-written input (unknown tokens land in the note, nothing dropped); it
sanitizes untrusted input; it rejects parts that would break the grammar; and
it is actually smaller than the JSON it replaces (measured, not asserted on
faith)."""

import json

import pytest

from hallpass import flex
from hallpass.flex import FlexError, Message, encode, parse

ESC = "\x1b"


def test_encode_shape():
    msg = Message(
        kind="task",
        to=("alice", "bob"),
        refs=("PR-42",),
        fields={"pri": "high"},
        note="resize the batch-7 images",
    )
    assert encode(msg) == "task @alice @bob #PR-42 pri=high | resize the batch-7 images"


def test_round_trips_full_message():
    msg = Message(
        kind="status",
        to=("all",),
        refs=("AIOPS-1", "PR-9"),
        fields={"st": "wip", "pct": "40"},
        note="halfway through the sweep | still going",  # note may contain the sep
    )
    assert parse(encode(msg)) == msg


def test_round_trips_minimal_message():
    for msg in (
        Message(kind="ack"),
        Message(kind="info", note="just a heads up"),
        Message(kind="ping", to=("gw",)),
        Message(kind="ref", refs=("x1",)),
        Message(kind="set", fields={"k": "v"}),
    ):
        assert parse(encode(msg)) == msg


def test_fields_emit_in_sorted_order_for_stability():
    a = encode(Message(kind="k", fields={"b": "2", "a": "1"}))
    b = encode(Message(kind="k", fields={"a": "1", "b": "2"}))
    assert a == b == "k a=1 b=2"


def test_parse_is_tolerant_of_handwritten_input():
    # unknown bareword tokens are preserved in the note, not dropped
    m = parse("task do the thing @alice pri=high")
    assert m.kind == "task"
    assert m.to == ("alice",)
    assert m.fields == {"pri": "high"}
    assert "do the thing" in m.note  # nothing lost


def test_parse_sanitizes_untrusted_input():
    m = parse(f"status @a{ESC}[31m st=ok | all{ESC}[0m good")
    assert m.kind == "status"
    assert m.to == ("a",)  # escape stripped, not part of the handle
    assert ESC not in m.note


def test_field_value_may_contain_equals():
    m = parse("cfg url=a=b=c")
    assert m.fields == {"url": "a=b=c"}
    assert encode(m) == "cfg url=a=b=c"


def test_encode_rejects_whitespace_in_structural_parts():
    for bad in (
        Message(kind="two words"),
        Message(kind="k", to=("a b",)),
        Message(kind="k", refs=("x y",)),
        Message(kind="k", fields={"key": "two words"}),
    ):
        with pytest.raises(FlexError):
            encode(bad)


def test_flex_is_smaller_than_equivalent_json():
    msg = Message(
        kind="task",
        to=("alice", "bob"),
        refs=("PR-42",),
        fields={"pri": "high", "due": "today"},
        note="resize the batch-7 images",
    )
    wire = encode(msg)
    as_json = json.dumps(
        {
            "kind": msg.kind,
            "to": list(msg.to),
            "refs": list(msg.refs),
            "fields": msg.fields,
            "note": msg.note,
        },
        separators=(",", ":"),  # give JSON its most compact form
    )
    # bytes as a tokenizer-agnostic proxy; FLEX drops braces/quotes/keys overhead
    assert len(wire) < len(as_json)
    # and it's a real saving, not a rounding artefact
    assert len(wire) <= 0.75 * len(as_json)


def test_module_is_exposed_and_usable_over_the_bus():
    from hallpass import A2ABus, ChannelPolicy, Principal

    bus = A2ABus()
    bus.declare_channel("build", ChannelPolicy())
    sender = Principal("orch", frozenset())
    bus.post(sender, "build", flex.encode(Message(kind="task", to=("w1",), note="go")))
    got = [
        flex.parse(m.body) for m in bus.catch_up(Principal("w1", frozenset()), "build")
    ]
    assert got[0].kind == "task" and got[0].to == ("w1",) and got[0].note == "go"
    bus.close()
