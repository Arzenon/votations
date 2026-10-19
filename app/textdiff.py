"""Word-level diff between two versions of a field, for the revision log.

Word level rather than line level, since the fields are paragraphs. Returns
segments, not HTML, so the template escapes the text as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache, partial

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

# Above this many characters on either side of the changed region, the diff
# isn't drawn on the results page but on the revision's own page. Bounds a page
# of 20 revisions to about a second.
INLINE_DIFF_CHARS = 20_000

# Words and whitespace as separate tokens, so paragraph breaks are preserved.
_TOKENS = re.compile(r"\s+|\S+")

# Each word with the whitespace before it, for changed regions over
# FINE_DIFF_TOKENS. difflib's cost grows with how often tokens repeat, and the
# spaces of `_TOKENS` are half of all tokens: on 50,000 characters this takes
# 0.3 s instead of 36 s. Only cost: a change at the field's very start also
# marks its first word.
_WORD_TOKENS = re.compile(r"\s*\S+|\s+")
FINE_DIFF_TOKENS = 1000


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
    # Too large to diff on the results page (see INLINE_DIFF_CHARS).
    deferred: bool = False


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


def _common_ends(before: list[str], after: list[str]) -> tuple[int, int]:
    """Number of tokens the two versions share at the start, then at the end."""
    head = 0
    limit = min(len(before), len(after))
    while head < limit and before[head] == after[head]:
        head += 1
    # Leave a trailing space to the change: an insertion reads " très grandes",
    # not "très grandes ".
    if head and not before[head - 1].strip():
        head -= 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    return head, tail


def diff(old: str, new: str, *, max_chars: int | None = None) -> list[Segment] | None:
    """Word-level diff between two versions of a field.

    None if the changed region has more than ``max_chars`` characters on a side.
    """
    # Lazy import: only the revision log needs difflib.
    import difflib

    before = _TOKENS.findall(old or "")
    after = _TOKENS.findall(new or "")
    # Linear, and leaves difflib only the region that changed.
    head, tail = _common_ends(before, after)
    middle_before = before[head : len(before) - tail]
    middle_after = after[head : len(after) - tail]
    if max_chars is not None and max(_chars(middle_before), _chars(middle_after)) > max_chars:
        return None
    if max(len(middle_before), len(middle_after)) > FINE_DIFF_TOKENS:
        middle_before = _WORD_TOKENS.findall("".join(middle_before))
        middle_after = _WORD_TOKENS.findall("".join(middle_after))

    out: list[Segment] = _equal_segments(before[:head]) if head else []
    # autojunk would ignore frequent tokens like spaces and "de", "la".
    matcher = difflib.SequenceMatcher(a=middle_before, b=middle_after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(_equal_segments(middle_before[i1:i2]))
            continue
        # A replacement is shown as a deletion followed by an insertion.
        if tag in ("replace", "delete"):
            out.append(Segment("removed", "".join(middle_before[i1:i2])))
        if tag in ("replace", "insert"):
            out.append(Segment("added", "".join(middle_after[j1:j2])))
    if tail:
        out.extend(_equal_segments(before[len(before) - tail :]))
    return out


def _chars(tokens: list[str]) -> int:
    return sum(len(token) for token in tokens)


def field_change(old: str | None, new: str | None, *, inline: bool = True) -> FieldChange:
    """Build the display for one revision: short values as is, the rest diffed.

    ``inline`` (the results page) leaves large changes to the revision's own
    page, which passes False.
    """
    old, new = old or "", new or ""
    short = (
        len(old) <= SHORT_VALUE_CHARS
        and len(new) <= SHORT_VALUE_CHARS
        and "\n" not in old
        and "\n" not in new
    )
    if short:
        return FieldChange(short=True, rewritten=False, old=old, new=new, segments=[])

    segments = diff(old, new, max_chars=INLINE_DIFF_CHARS if inline else None)
    if segments is None:
        return FieldChange(
            short=False, rewritten=False, old=old, new=new, segments=[], deferred=True
        )
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


# Revisions never change, so the revision page computes each diff once per process.
full_change = lru_cache(maxsize=32)(partial(field_change, inline=False))
