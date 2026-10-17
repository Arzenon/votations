"""Word-level diff between two versions of a field, for the revision log.

Word level rather than line level, since the fields are paragraphs. Returns
segments, not HTML, so the template escapes the text as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Short single-line values (flags, dates, references) are shown as
# "before / after" instead of diffed. Real sentences are longer than this.
SHORT_VALUE_CHARS = 40

# Unchanged runs longer than this many words are collapsed.
ELIDE_OVER_WORDS = 40

# Words of context kept on each side of a collapsed run.
CONTEXT_WORDS = 12

# Above this share of changed words, show both versions instead of a diff,
# which becomes unreadable for a rewrite.
REWRITE_SHARE = 0.5

# Words and whitespace as separate tokens, so paragraph breaks are preserved.
_TOKENS = re.compile(r"\s+|\S+")


@dataclass(frozen=True)
class Segment:
    """One run of the diff."""

    # "equal", "added", "removed" or "elided"
    kind: str
    text: str = ""
    # For "elided": number of collapsed words.
    words: int = 0


@dataclass(frozen=True)
class FieldChange:
    """How to display one recorded change."""

    short: bool
    # Too much changed for a word diff (see REWRITE_SHARE).
    rewritten: bool
    old: str
    new: str
    segments: list[Segment]


def _words(tokens: list[str]) -> int:
    return sum(1 for t in tokens if t.strip())


def _take(tokens: list[str], count: int, *, from_end: bool = False) -> list[str]:
    """The first (or last) tokens containing ``count`` words."""
    ordered = reversed(tokens) if from_end else iter(tokens)
    out: list[str] = []
    seen = 0
    for token in ordered:
        if token.strip():
            if seen == count:
                break
            seen += 1
        out.append(token)
    return out[::-1] if from_end else out


def _equal_segments(tokens: list[str]) -> list[Segment]:
    """An unchanged run, collapsed in the middle if long."""
    total = _words(tokens)
    if total <= ELIDE_OVER_WORDS:
        return [Segment("equal", "".join(tokens))]
    head = _take(tokens, CONTEXT_WORDS)
    tail = _take(tokens, CONTEXT_WORDS, from_end=True)
    return [
        Segment("equal", "".join(head)),
        Segment("elided", words=total - _words(head) - _words(tail)),
        Segment("equal", "".join(tail)),
    ]


def diff(old: str, new: str) -> list[Segment]:
    """Word-level diff between two versions of a field."""
    # Lazy import: only the revision log needs difflib.
    import difflib

    before = _TOKENS.findall(old or "")
    after = _TOKENS.findall(new or "")
    # autojunk would ignore frequent tokens like spaces and "de", "la".
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)

    out: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(_equal_segments(before[i1:i2]))
            continue
        # A replacement is shown as a deletion followed by an insertion.
        if tag in ("replace", "delete"):
            out.append(Segment("removed", "".join(before[i1:i2])))
        if tag in ("replace", "insert"):
            out.append(Segment("added", "".join(after[j1:j2])))
    return out


def field_change(old: str | None, new: str | None) -> FieldChange:
    """Build the display for one revision: short values as is, the rest diffed."""
    old, new = old or "", new or ""
    short = (
        len(old) <= SHORT_VALUE_CHARS
        and len(new) <= SHORT_VALUE_CHARS
        and "\n" not in old
        and "\n" not in new
    )
    if short:
        return FieldChange(short=True, rewritten=False, old=old, new=new, segments=[])

    segments = diff(old, new)
    touched = sum(
        _words(_TOKENS.findall(s.text)) for s in segments if s.kind in ("added", "removed")
    )
    total = touched + sum(
        _words(_TOKENS.findall(s.text)) + s.words for s in segments if s.kind in ("equal", "elided")
    )
    rewritten = bool(total) and touched / total > REWRITE_SHARE
    return FieldChange(
        short=False, rewritten=rewritten, old=old, new=new, segments=[] if rewritten else segments
    )
