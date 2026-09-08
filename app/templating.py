"""Shared Jinja2 templates instance, filters and context processors."""

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.auth.base import get_auth_provider
from app.config import get_settings
from app.labels import (
    CHOICE_LABELS,
    field_label,
    french_date,
    french_datetime,
    home_band,
    import_status,
    law_status,
)
from app.security import safe_url

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# What the site is, in one sentence. Used as the default meta description — the
# line search engines and messaging apps show under the link — and as the lede on
# the home page. Defined once because it was drifting: the two copies said nearly
# the same thing in not quite the same words.
TAGLINE = (
    "Donnez votre avis sur les lois adoptées par le Parlement : données "
    "ouvertes, aucune donnée personnelle collectée."
)

# The permanent transparency disclaimer, in two lengths (charter requirement).
#
# The admission itself is on every page and is never behind a click: this platform's
# whole claim is that it says what it is worth, and a limitation a reader has to ask
# for is not one that has been stated. What varies is how much of the *consequence*
# comes with it.
#
# `DISCLAIMER_SHORT` is the banner everywhere: it names the flaw — self-selection —
# and stops there. `DISCLAIMER` adds what follows from it, and is used on the pages
# that actually publish figures, where the mistake it prevents is live: reading a
# percentage on this site as a percentage of the French population.
#
# The split is a measurement, not a preference. The full sentence ran to 154 px on a
# 375 px screen — a fifth of the first thing a visitor ever sees, above the heading,
# on every page including the ones carrying no number at all. The short form costs
# 87 px. Nothing about the limitation was traded away for those 67 px; the sentence
# that explains it simply now sits where the figure it qualifies is.
DISCLAIMER_SHORT = (
    "Votations Françaises n'est pas un sondage représentatif : les personnes qui "
    "participent se sélectionnent elles-mêmes."
)

DISCLAIMER = (
    "Votations Françaises n'est pas un sondage représentatif. Les personnes qui "
    "participent se sélectionnent elles-mêmes : les résultats expriment l'opinion "
    "des personnes inscrites sur la plateforme et ne peuvent pas être généralisés "
    "à la population française."
)


def _auth_context(request: Request) -> dict:
    """Resolve the auth provider per render, not once at import time.

    Binding it into the globals at import made it impossible to swap the provider
    at all, which is the whole point of the seam. Deliberately no date attached:
    the provider that would replace the anonymous one is not a milestone the code
    reaches — see :mod:`app.auth.base`.
    """
    return {"auth": get_auth_provider()}


def _page_context(request: Request) -> dict:
    """Where this page lives, absolutely — for the tags that cannot be relative.

    ``site_origin`` is the scheme and host this request arrived on, which is the
    only place that is known: the domain is not configuration, and a fork or a
    second instance must describe itself rather than inherit these values. It is
    correct behind the proxy because uvicorn is run with ``--proxy-headers`` and
    Caddy sets ``X-Forwarded-Proto`` — checked against production, where every
    absolute URL the site emits is ``https://``.

    ``canonical_url`` is that origin plus the path, **deliberately without the
    query string**. Every parameter this site accepts either narrows a listing
    (``?q=``, ``?page=``) or turns a page of a sub-table (``?revisions=``), and
    none of them produces a page worth ranking separately from the one it varies:
    left alone, `/loi/x/resultats` would offer a search engine one near-identical
    URL per page of its modification history. A page that genuinely needs another
    answer overrides the ``canonical`` block.
    """
    origin = str(request.base_url).rstrip("/")
    return {"site_origin": origin, "canonical_url": f"{origin}{request.url.path}"}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR), context_processors=[_auth_context, _page_context]
)

# Starlette builds the environment without saying, so Jinja's own default applies
# and every render stats every template it touches to see whether it has changed.
# In production they cannot: the image is what runs. In development they change
# constantly, which is the whole point of `--reload`.
templates.env.auto_reload = not get_settings().is_production

templates.env.globals["DISCLAIMER"] = DISCLAIMER
templates.env.globals["DISCLAIMER_SHORT"] = DISCLAIMER_SHORT
templates.env.globals["TAGLINE"] = TAGLINE
# The three choices in ballot order, so the bars, the two result tables and the
# ballot itself iterate one mapping instead of each writing « Pour / Contre /
# Abstention » out again. See `app.labels.CHOICE_LABELS`.
templates.env.globals["CHOICE_LABELS"] = CHOICE_LABELS


def _digest(name: str) -> str:
    """A short content fingerprint of one static file, or "" if unreadable."""
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


_cached_digest = lru_cache(maxsize=64)(_digest)


def asset_url(name: str) -> str:
    """``/static/<name>`` with a content fingerprint appended.

    The static files are served with a one-year immutable cache, which is only
    safe if the URL changes when the file does. Hashing the bytes keeps that true
    with no build step: a deployed edit produces a new URL, and an unchanged file
    keeps its cached copy.

    The hash is memoised in production only, where the files genuinely cannot
    change under a running server. In development they change constantly, and
    `--reload` restarts on Python edits alone — so a cached fingerprint plus an
    immutable Cache-Control meant an edited stylesheet kept its old URL and the
    browser kept serving the old bytes. The change appeared not to have happened.
    """
    digest = _cached_digest(name) if get_settings().is_production else _digest(name)
    return f"/static/{name}?v={digest}" if digest else f"/static/{name}"


templates.env.globals["asset_url"] = asset_url


def paragraphs(value: str | None) -> list[str]:
    """Split a text block into paragraphs on blank lines.

    The presentation blocks are written (and drafted) as several paragraphs
    separated by an empty line; rendering them into a single ``<p>`` collapsed
    every one of them into a wall of text.
    """
    if not value:
        return []
    return [block.strip() for block in value.replace("\r\n", "\n").split("\n\n") if block.strip()]


#: How many of « Ce que ça change en pratique » stand above the fold.
#:
#: The block is ordered most-concerned-first — the drafting prompt asks for that
#: explicitly — so the first few paragraphs are the ones addressed to the largest
#: number of readers, and taking them from the top is not an arbitrary cut.
VISIBLE_AUDIENCES = 5

#: Fold only when folding earns the control it adds. Hiding one or two paragraphs
#: behind a click asks the reader to work for nothing; the fold exists for the
#: laws where it genuinely changes the shape of the page.
_WORTH_FOLDING = 3


def practical_split(value: str | None) -> tuple[list[str], list[str]]:
    """« Ce que ça change en pratique », as (what is shown, what is folded).

    A law that amends sixty articles across one sector really does concern twenty
    different publics, and the charter forbids picking which of them go
    unmentioned — so the block cannot be shortened by dropping anyone. What it can
    do is stop being a wall: the publics that concern the most people stay in
    view, the rest sit one click away, and nothing is left out.

    That is deliberately a change to the *page* rather than to the drafting
    instructions. Three successive attempts to hold the block short by prompt
    failed for the same reason each time — « ne supprime jamais la mesure » and
    « deux ou trois phrases » cannot both hold for a sixty-article law, and the
    model resolved it in favour of completeness every time. Which is the right
    instinct for this platform, so the page accommodates it instead.
    """
    blocks = paragraphs(value)
    if len(blocks) < VISIBLE_AUDIENCES + _WORTH_FOLDING:
        return blocks, []
    return blocks[:VISIBLE_AUDIENCES], blocks[VISIBLE_AUDIENCES:]


def practical_layout(value: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    """« Ce que ça change en pratique », as (rows in page order, folded tail).

    Two shapes, and which one applies is decided by the drafting rather than by a
    setting. When the paragraphs carry the labels the prompt asks for, each becomes
    a row a reader opens for themselves: the whole block collapses to a list of
    publics, every one of them named, none behind a counter. That is the shape this
    page is built for — it turns the longest block of the page into something
    scannable, and it is *more* faithful to the charter than the fold it replaces,
    which hid the narrower publics behind « N autres publics concernés ».

    When no paragraph carries a label, the rows cannot be built and the block falls
    back to :func:`practical_split` — the wall treatment, unchanged. That is not a
    legacy branch kept for symmetry: a law drafted before the labels existed, or one
    the model wrote in another shape, still has to render, and rendering it as one
    long unfoldable list would be worse than what it does today.

    Labels are never invented for a paragraph that lacks one. A paragraph with no
    label renders plainly, in place, among the rows — which makes the drafting
    failure visible to whoever reviews the law before publication, rather than
    silently averaged into the page.
    """
    rows = [audience_split(para) for para in paragraphs(value)]
    if any(label for label, _ in rows):
        return rows, []
    shown, folded = practical_split(value)
    return [("", para) for para in shown], folded


def summary_split(value: str | None) -> tuple[str, list[str]]:
    """« Ce que dit la loi », as (the opening paragraph, the rest).

    The opening paragraph is written to stand on its own — the drafting prompt
    requires it in as many words — and was rendered like every other paragraph,
    so the one sentence a hesitating reader most needs looked exactly like the
    detail underneath it. The page lifts it into an encart instead.

    **The first paragraph is only lifted when there is something left behind.**
    A law summarised in a single paragraph would otherwise leave « Ce que dit la
    loi » empty, with its whole content moved into a box above a heading that now
    introduces nothing. In that case the block keeps it and the encart carries
    only the debate.
    """
    blocks = paragraphs(value)
    if len(blocks) < 2:
        return "", blocks
    return blocks[0], blocks[1:]


#: Longest an audience label may be before it stops being one.
#:
#: « Pour les employeurs » is a label. « La règle est simple » is a sentence that
#: happens to reach a colon, and promoting it would set apart a statement rather
#: than mark a section — which is the whole distinction this filter rests on.
#:
#: This was 45, on the belief that « real labels run to twenty or thirty
#: characters ». Measured against the four laws actually drafted, that belief was
#: wrong: the 23 punctuation-free labels run from 9 to 55 characters, and the bound
#: was rejecting three legitimate publics on the loi de programmation militaire
#: alone — « Si vous travaillez dans un secteur scientifique protégé » (55), « Si
#: vous travaillez dans un établissement scolaire » (49), « Si vous êtes agent d'un
#: service de renseignement » (48). None of the three can be shortened without
#: naming the institution instead of the reader, which is the opposite of what a
#: public is supposed to be.
#:
#: The same measurement showed what really separates labels from prose here:
#: *everything* longer than 55 characters in that corpus carries punctuation, up to
#: whole 1 180-character paragraphs. `_NOT_IN_A_LABEL` is doing the work; this bound
#: is the backstop for the case it cannot catch — a long clause with no punctuation
#: at all. 75 leaves the observed maximum 20 characters of room and still refuses
#: anything that reads as a sentence.
MAX_AUDIENCE_CHARS = 75

#: Punctuation a label does not contain. A label is a short noun phrase; anything
#: carrying sentence punctuation — a comma included — is prose that happened to
#: arrive at a colon.
#:
#: The comma is here because of a test that caught it: « La règle est simple, et
#: elle tient en peu de mots : … » is short enough and was being promoted. It
#: costs the occasional real label — « Pour les communes, les départements et les
#: régions » renders plain — and that is the right way round. A false positive
#: puts bold on the substance of a law, which is the one thing this platform must
#: not do; a false negative renders a paragraph exactly as it did last week.
_NOT_IN_A_LABEL = ".?!;,"


def audience_split(paragraph: str) -> tuple[str, str]:
    """One paragraph of « Ce que ça change en pratique », as (label, rest).

    The block is already organised by *personnes concernées* — one paragraph per
    audience, most numerous first, each opening with who it is addressed to. The
    prompt has asked for that from the start. The page rendered it as six
    identical grey paragraphs, so a reader looking for themselves had to read the
    first line of every one.

    Emphasising the label is navigation, not editorial. That distinction is the
    reason this filter exists at all and the reason it stops here: marking *what
    matters* inside a law's substance would be choosing for the reader, on a
    platform whose first rule is that it does not. « **Pour les employeurs** »
    says « this one is about you » and nothing whatsoever about the measure.

    Returns ``("", paragraph)`` when the paragraph does not open with a label, so
    a block drafted in another shape renders exactly as it does today.
    """
    head, colon, rest = paragraph.partition(" : ")
    if not colon or len(head) > MAX_AUDIENCE_CHARS or not rest.strip():
        return "", paragraph
    if any(mark in head for mark in _NOT_IN_A_LABEL):
        return "", paragraph
    return head, rest


templates.env.filters["paragraphs"] = paragraphs
templates.env.filters["summary_split"] = summary_split
templates.env.filters["audience_split"] = audience_split
templates.env.filters["practical_split"] = practical_split
templates.env.filters["practical_layout"] = practical_layout
templates.env.filters["law_status"] = law_status
templates.env.filters["home_band"] = home_band
templates.env.filters["import_status"] = import_status
templates.env.filters["field_label"] = field_label
templates.env.filters["french_date"] = french_date
templates.env.filters["french_datetime"] = french_datetime
# Defence in depth. The admin form refuses a non-http(s) link, but that is one
# gate on one path: seed data, a migration, a direct database edit or a future
# importer could all put a `javascript:` URL in a column that templates render as
# an href. Sanitising again at render means a stored payload is inert wherever it
# came from, and an empty result lets the template drop the link entirely.
templates.env.filters["safe_href"] = safe_url
