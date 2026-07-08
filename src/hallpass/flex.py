"""FLEX: a token-efficient message language for the A2A layer.

Agents talking to each other over ``A2ABus`` send strings. Free prose is
expensive (every token is context and money) and unreliable to parse; JSON is
structured but pays for braces, quotes, and repeated key punctuation on every
message. FLEX (Fielded Lightweight EXchange) is a one-line format that keeps the
structure and drops the overhead:

    <kind> [@recipient]* [#ref]* [key=value]* [ | free-text note]

    task @alice @bob #PR-42 pri=high | resize the batch-7 images

- ``kind`` -- one bareword for intent (task, status, answer, question, info,
  ack, ... an open set; you pick your vocabulary).
- ``@recipient`` -- zero or more addressees (``@all`` is just a recipient named
  "all"). Order preserved.
- ``#ref`` -- zero or more references: a ticket, PR, or prior message id.
- ``key=value`` -- zero or more typed fields; the value is whitespace-free
  (put anything longer in the note).
- `` | `` -- introduces a free-text note; everything after it is prose.

It rides the existing bus unchanged: ``bus.post(p, ch, encode(msg))`` and
``parse(m.body)`` on the way out. ``parse`` is tolerant of hand-written input
and runs the body through hallpass's sanitizer first, since an inbound message
is untrusted. ``parse(encode(m)) == m`` for any message ``encode`` accepts.

This is hallpass's own grammar, defined here; it is not an implementation of
any other system's format.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sanitize import sanitize

__all__ = ["Message", "encode", "parse", "FlexError"]

_NOTE_SEP = " | "


class FlexError(Exception):
    """A message could not be encoded because a part would break the grammar
    (whitespace in a kind, recipient, ref, or field value). The message names
    the offending part; it never contains a note body."""


@dataclass(frozen=True)
class Message:
    kind: str
    to: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)
    note: str = ""


def _no_ws(label: str, value: str) -> str:
    if value == "" or any(c.isspace() for c in value):
        raise FlexError(f"{label} must be a non-empty whitespace-free token")
    return value


def encode(message: Message) -> str:
    """Render a Message to its FLEX wire string. Fields are emitted in sorted
    key order so the output is stable (deterministic for tests and caches)."""
    parts = [_no_ws("kind", message.kind)]
    parts += [f"@{_no_ws('recipient', r)}" for r in message.to]
    parts += [f"#{_no_ws('ref', r)}" for r in message.refs]
    for key in sorted(message.fields):
        parts.append(
            f"{_no_ws('field key', key)}={_no_ws('field value', message.fields[key])}"
        )
    header = " ".join(parts)
    if message.note:
        return f"{header}{_NOTE_SEP}{message.note}"
    return header


def parse(text: str) -> Message:
    """Parse a FLEX string into a Message. Tolerant: unrecognised header tokens
    fall into the note rather than being dropped, so nothing is silently lost.
    The input is sanitized first (it is untrusted agent output)."""
    text = sanitize(text)
    header, sep, note = text.partition(_NOTE_SEP)
    tokens = header.split()
    kind = ""
    to: list[str] = []
    refs: list[str] = []
    fields: dict[str, str] = {}
    extra: list[str] = []
    if tokens:
        kind = tokens[0]
        for tok in tokens[1:]:
            if tok.startswith("@") and len(tok) > 1:
                to.append(tok[1:])
            elif tok.startswith("#") and len(tok) > 1:
                refs.append(tok[1:])
            elif "=" in tok and not tok.startswith("="):
                key, _, value = tok.partition("=")
                fields[key] = value
            else:
                extra.append(tok)  # unrecognised -> preserved in the note
    note_parts = ([" ".join(extra)] if extra else []) + ([note] if sep else [])
    return Message(
        kind=kind,
        to=tuple(to),
        refs=tuple(refs),
        fields=fields,
        note=" ".join(p for p in note_parts if p).strip(),
    )
