"""Treat inbound agent-to-agent message text as untrusted data.

An agent reading a channel is reading text another principal wrote, and that
text usually lands in a model's context. That is an injection surface. hallpass
cannot decide for you whether the *words* are an attack -- semantic
prompt-injection detection is not something to promise. What it can do, and does
here, is remove the tricks that let text hide or spoof what it is: terminal
escape sequences (CSI/OSC and the string-terminated DCS/SOS/PM/APC families),
other control characters, Unicode bidi overrides (the Trojan-Source reordering
attack), and zero-width / invisible format characters (including the Unicode tag
block used to smuggle invisible instructions to a model). ``frame_untrusted``
then wraps the cleaned text in an explicit boundary so a model is told, in-band,
that the content is data and not instructions.

Honest about scope: this neutralizes control/escape/spoofing tricks and bounds
length. It does not, and cannot, guarantee that the plain words inside are safe
to act on -- that is why the framing says "data."

Character removals are defined by codepoint number, not literal characters, so
this source file never contains the invisible/bidi bytes it exists to strip.
"""

from __future__ import annotations

import re

__all__ = ["sanitize", "frame_untrusted"]

# Multi-character terminal escape sequences. Order matters: the string-terminated
# families (DCS/SOS/PM/APC, introduced by ESC P/X/^/_) come first so their whole
# payload is consumed rather than left behind as visible text.
_ESCAPES = re.compile(
    r"\x1b[PX^_][^\x1b\x07]*(?:\x1b\\|\x07)?"  # DCS/SOS/PM/APC ... ST or BEL
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"  # any remaining two-char escape
)


def _strip_codepoints() -> frozenset[int]:
    points: set[int] = set()
    points |= set(range(0x00, 0x09))  # C0 controls, keeping...
    points |= {0x0B, 0x0C}  # ...tab (0x09) and newline (0x0A)
    points |= set(range(0x0E, 0x20))
    points |= set(range(0x7F, 0xA0))  # DEL + C1 block
    points |= set(range(0x202A, 0x202F))  # bidi embeddings/overrides
    points |= set(range(0x2066, 0x206A))  # bidi isolates
    # Zero-width / invisible format chars. ZWNJ (0x200C) and ZWJ (0x200D) are
    # intentionally absent: load-bearing in emoji and Arabic/Indic scripts.
    points |= {0x200B, 0x200E, 0x200F, 0x00AD, 0x180E, 0xFEFF}
    points |= set(range(0x2060, 0x2065))  # word joiner + invisible operators
    points |= set(range(0xE0000, 0xE0080))  # Unicode tag block: smuggling channel
    return frozenset(points)


_STRIP = _strip_codepoints()

_LABEL = re.compile(r"[a-z][a-z0-9-]{0,63}")

_DEFAULT_MARKER = " ...[truncated]"


def sanitize(text: str, *, max_length: int | None = None) -> str:
    """Return ``text`` with terminal escape sequences, control characters,
    bidi overrides, and zero-width/invisible format characters removed (tabs
    and newlines are kept; CR/CRLF are normalised to LF; ZWJ/ZWNJ and emoji
    variation selectors are preserved). When ``max_length`` is given, the
    result is truncated to it with a visible marker, so an oversized body
    cannot flood a reader's context silently."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ESCAPES.sub("", text)
    text = "".join(ch for ch in text if ord(ch) not in _STRIP)
    if max_length is not None and len(text) > max_length:
        keep = max(max_length - len(_DEFAULT_MARKER), 0)
        text = text[:keep] + _DEFAULT_MARKER
    return text


def frame_untrusted(text: str, *, label: str = "untrusted-message") -> str:
    """Sanitize ``text`` and wrap it in an explicit boundary that marks it as
    data, not instructions, for a model that will read it. Any occurrence of
    the boundary tag inside the text -- opening or closing, in any case, with
    or without inner whitespace -- is defanged so the content cannot forge the
    frame and escape into instruction context. ``label`` must be a simple slug;
    a caller cannot inject through it."""
    if not _LABEL.fullmatch(label):
        raise ValueError("label must match [a-z][a-z0-9-]{0,63}")
    clean = sanitize(text)
    tag = re.compile(rf"<\s*/?\s*{re.escape(label)}\s*>", re.IGNORECASE)
    # Replace the angle brackets of any forged tag with look-alikes (guillemets)
    # so it can no longer act as a real frame boundary.
    clean = tag.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), clean)
    return f"<{label}>\n{clean}\n</{label}>"
