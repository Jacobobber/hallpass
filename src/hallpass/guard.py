"""Bound the size of a tool result before it reaches the model.

A single connector call can return a firehose -- list every issue, dump a big
document -- and that whole payload lands in the caller's context window. Left
unchecked it silently crowds out everything else the agent was doing. The wrong
fix is to truncate and stay quiet: the agent then reasons over a partial result
believing it is whole. ``guard_response`` instead replaces an oversized result
with an explicit envelope that says, in-band, "this was too big, here is a
preview, narrow your query" -- so the model knows it saw a fraction and how to
get less. Pagination stays the underlying tool's job (its own limit/cursor
params); the guard's job is to make the overflow impossible to miss.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["guard_response", "TRUNCATED_KEY"]

TRUNCATED_KEY = "hallpass:truncated"

# Room reserved inside the byte budget for the envelope's own keys, so the
# returned object stays under max_bytes including its metadata, not just the
# preview.
_ENVELOPE_OVERHEAD = 512


def _serialize(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def guard_response(value: Any, *, max_bytes: int) -> Any:
    """Return ``value`` unchanged if its serialized size is within
    ``max_bytes``; otherwise return a truncation envelope carrying the byte
    counts, a UTF-8-safe preview of the start, and guidance to re-query with
    narrower parameters. Never silently drops data -- an overflow is always
    visible via ``TRUNCATED_KEY``."""
    serialized = _serialize(value)
    size = len(serialized.encode("utf-8"))
    if size <= max_bytes:
        return value
    budget = max(max_bytes - _ENVELOPE_OVERHEAD, 0)
    preview = serialized.encode("utf-8")[:budget].decode("utf-8", "ignore")
    return {
        TRUNCATED_KEY: True,
        "reason": (
            "response exceeded the server's size limit; it was not returned in "
            "full. Re-call the tool with narrower parameters (a limit, page, "
            "filter, or date range) to retrieve less at once."
        ),
        "bytes": size,
        "max_bytes": max_bytes,
        "preview": preview,
    }
