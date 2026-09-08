"""What changed between two versions of a field, word by word.

The public record of changes used to print both values in full, side by side, in
two 22rem cells of small grey text. Everything needed to check an edit was on the
page and none of it was legible: a reworded sentence inside a 4 000-character
presentation block looked exactly like a block that had been rewritten, and the
reader had to find the difference themselves. On the one section whose entire
purpose is « judge whether the votes were cast on the text shown today », that is
the whole feature failing quietly.

So the difference is computed here instead, and the page marks it.

**Word level, not line level.** A `git`-style line diff is right for code and
wrong for prose: these fields are paragraphs, so changing one word marks the
whole paragraph removed and re-added, which is the wall of text again with
colours on it. Splitting on words puts the marking where the edit was.

**Data, never markup.** These functions return segments; `results.html` renders
them through Jinja's ordinary escaping. Building `<ins>`/`<del>` here would mean
hand-escaping administrator-authored text into HTML, which is the one thing this
codebase refuses to do anywhere else — see `app.security.safe_url` and the
`safe_href` filter for the same reflex applied to URLs.

`difflib` is the standard library, so this adds no dependency and nothing runs in
the browser: the page still works with scripting off, as the rest of the site
does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Both values shorter than this, and single-line, are shown as « avant → après »
#: rather than diffed. Marking one word removed and another added inside « oui »
#: → « non » is noise around a change the reader can already see whole.
#:
#: Forty rather than a rounder eighty, and the line is drawn where prose starts:
#: a value this short is a *token* — the publication flag, one of the three
#: dates, an official reference — while no real sentence fits in it. Eighty put
#: « La loi impose aux plateformes de publier leurs critères. » on the wrong side
#: of it and showed two nearly identical sentences instead of marking the three
#: words between them.
SHORT_VALUE_CHARS = 40

#: An unchanged run longer than this many words is collapsed to a count.
#:
#: Nothing is hidden by it that the diff was showing: an « equal » run is by
#: definition the text both versions carry, so the reader is not being kept from
#: a difference — they are being kept from re-reading a paragraph that did not
#: move. A full rewrite of a 20 000-character block would otherwise print the old
#: and the new one after the other, which is worse than what this replaces.
ELIDE_OVER_WORDS = 40

#: Words kept either side of a collapsed run, so a change never appears without
#: the sentence it sits in.
CONTEXT_WORDS = 12

#: Past this share of words touched, the block is shown whole instead of marked.
#:
#: A word diff is the right picture of an *edit* and the wrong picture of a
#: rewrite: when most of the words move, the marks stop being signal and become
#: confetti — on the real corpus a rewritten « ce qui a été débattu » produced
#: fifty-odd alternating marks in one paragraph, with deletions and insertions
#: abutting so that « garanties » and « garde-fous » read as one word. GitLab
#: draws the same line, refusing to word-highlight lines it considers too
#: different and showing them as removed and added instead.
#:
#: Half is the point where the diff stops being mostly context.
REWRITE_SHARE = 0.5

#: Words and the whitespace between them, kept as separate tokens so the text can
#: be put back together exactly — paragraph breaks included, which matter here
#: because these fields are prose and `app.templating.paragraphs` splits on them.
_TOKENS = re.compile(r"\s+|\S+")


@dataclass(frozen=True)
class Segment:
    """One run of the diff: unchanged text, an insertion, a deletion, or a gap."""

    #: "equal" | "added" | "removed" | "elided"
    kind: str
    text: str = ""
    #: For "elided" only: how many unchanged words were collapsed.
    words: int = 0


@dataclass(frozen=True)
class FieldChange:
    """How one recorded change should be shown.

    ``short`` is the escape hatch for values that are not prose. The template
    branches on it rather than on the field name: a list of field names here
    would be a fourth copy of something `app.labels.FIELD_LABELS` and
    `app.crud.laws.TRACKED_FIELDS` already spell, and it would be wrong the day a
    title happens to be long or a summary happens to be one line.
    """

    short: bool
    #: True when so much changed that marking it word by word is less readable
    #: than showing both versions — see :data:`REWRITE_SHARE`.
    rewritten: bool
    old: str
    new: str
    segments: list[Segment]


def _words(tokens: list[str]) -> int:
    return sum(1 for t in tokens if t.strip())


def _take(tokens: list[str], count: int, *, from_end: bool = False) -> list[str]:
    """The leading (or trailing) tokens holding ``count`` words."""
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
    """An unchanged run, collapsed in the middle when it is long enough to be."""
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
    """The word-level difference between two versions of a field."""
    # Imported here rather than at module scope: `difflib` is a few hundred
    # kilobytes of pure Python that only the results page's history section ever
    # needs, and every other page imports this module's siblings at boot.
    import difflib

    before = _TOKENS.findall(old or "")
    after = _TOKENS.findall(new or "")
    # `autojunk` drops tokens it considers too common to be worth matching, which
    # on ordinary French prose is every space and half the articles — it made
    # « de » and « la » read as changed wherever they appeared.
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)

    out: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(_equal_segments(before[i1:i2]))
            continue
        # A replacement is a deletion and an insertion, in that order, so the two
        # read as « this became that » rather than as two unrelated marks.
        if tag in ("replace", "delete"):
            out.append(Segment("removed", "".join(before[i1:i2])))
        if tag in ("replace", "insert"):
            out.append(Segment("added", "".join(after[j1:j2])))
    return out


def field_change(old: str | None, new: str | None) -> FieldChange:
    """How to render one row of the public record of changes.

    Short single-line values are handed back whole for a « before → after »
    rendering; anything longer is diffed.
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
