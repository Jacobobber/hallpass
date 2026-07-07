"""Tool search: retrieve the tools relevant to a query instead of listing all.

A bridge that fronts hundreds of tools cannot put every tool definition in
an agent's context; the agent has to search for the few it needs. hallpass
ranks only the tools a principal is already authorized to see, so search can
never surface a tool the caller could not call: the gate runs first, the
ranker second.

The default ranker is a zero-dependency BM25 over each tool's name and
description. Identifier names are split on camelCase and snake_case, so a
query like "read a note" matches a ``read_note`` tool. The ranker is
pluggable behind ``ToolRanker``: an embedding backend can replace it without
touching the gate.
"""

from __future__ import annotations

import math
import re
from typing import Protocol

from .gating import ToolSpec

__all__ = ["ToolRanker", "LexicalRanker", "tokenize"]

# Split on punctuation, whitespace, and underscore. ``\W`` is Unicode-aware
# for str patterns in Python 3, so letters and digits in any script (Cyrillic,
# CJK, accented Latin) are preserved and remain searchable.
_SPLIT = re.compile(r"[\W_]+")
# camelCase and acronym-to-word boundaries: readNote -> read Note,
# HTTPServer -> HTTP Server. These are ASCII-case boundaries; scripts without
# case simply do not trigger them, which is correct.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, splitting identifier boundaries so
    ``read_note`` and ``readNote`` both become ['read', 'note']. Unicode
    letters and digits are preserved, so non-Latin tool names stay
    searchable."""
    tokens: list[str] = []
    for chunk in _SPLIT.split(text):
        if not chunk:
            continue
        for piece in _CAMEL.split(chunk):
            if piece:
                tokens.append(piece.lower())
    return tokens


class ToolRanker(Protocol):
    def rank(self, query: str, tools: list[ToolSpec]) -> list[ToolSpec]:
        """Return ``tools`` ordered most relevant to ``query`` first. May
        drop tools judged irrelevant; must not add tools it was not given,
        so it cannot widen what the caller sees."""
        ...


class LexicalRanker:
    """Zero-dependency BM25 over each tool's name and description.

    Corpus statistics (document frequency, average length) are computed over
    the candidate set passed to ``rank`` at query time, so ranking always
    reflects exactly the tools the caller is allowed to see and never a
    global index that could rank in a tool they cannot access.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b

    def rank(self, query: str, tools: list[ToolSpec]) -> list[ToolSpec]:
        query_terms = tokenize(query)
        if not query_terms or not tools:
            return []
        docs = [tokenize(f"{t.name} {t.description}") for t in tools]
        n = len(docs)
        avgdl = sum(len(d) for d in docs) / n or 1.0
        doc_freq: dict[str, int] = {}
        for doc in docs:
            for term in set(doc):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        scored: list[tuple[float, int]] = []
        for i, doc in enumerate(docs):
            score = 0.0
            if doc:
                term_freq: dict[str, int] = {}
                for term in doc:
                    term_freq[term] = term_freq.get(term, 0) + 1
                dl = len(doc)
                for term in query_terms:
                    freq = term_freq.get(term, 0)
                    if freq == 0:
                        continue
                    idf = math.log(
                        1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5)
                    )
                    denom = freq + self._k1 * (1 - self._b + self._b * dl / avgdl)
                    score += idf * (freq * (self._k1 + 1)) / denom
            scored.append((score, i))

        # Relevance descending, original order as a stable tie-break; drop
        # tools nothing in the query matched.
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [tools[i] for score, i in scored if score > 0]
