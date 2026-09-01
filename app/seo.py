"""What each page says about itself, in schema.org's vocabulary.

Structured data is not a ranking trick and is not sold as one here. It changes
how a result is *displayed*, and it makes a page eligible for particular
surfaces — a breadcrumb line instead of a bare URL, and, for the open-data
exports, Google Dataset Search, which is a real audience for this project and
the one place a search engine can send somebody who wants the raw figures.

Three things follow from the charter and shape everything below.

**Nothing is asserted here that the page does not already show.** Every value is
read off the law: its title, its official reference, the day Parliament acted,
the link to the official text. Marking up something a reader cannot see is what
search engines call spam and what this platform would call lying.

**No licence is claimed.** The footer says the data is published « en accès
libre », and that is not the same thing as naming a licence. A `Dataset` without
a `license` is simply a dataset whose terms are unstated; one carrying a licence
nobody chose would be a legal claim invented by a template. Worth deciding —
Licence Ouverte would be the obvious fit, given that is what the Assemblée
nationale and the DILA publish under — and worth deciding on purpose.

**It is emitted as JSON-LD, which the Content-Security-Policy allows.** That is
not an accident of configuration: `script-src 'self'` blocks inline *scripts*,
and a `<script type="application/ld+json">` is a data block that no browser
executes. Verified rather than assumed — under this exact policy a real inline
script is refused while these blocks are read normally, with no violation
reported. So the site's structured data costs nothing in security posture, and
the CSP must not be loosened for it.

The dicts built here are rendered with Jinja's ``tojson``, which escapes ``<``,
``>``, ``&`` and ``'`` as unicode sequences — the reason a law title can be
dropped into a ``<script>`` block without a way out of it.
"""

from __future__ import annotations

from typing import Any

from app.models import Law
from app.security import safe_url

#: What the site calls itself wherever it has to name itself.
SITE_NAME = "Votations Françaises"

#: Everything here is written and read in French.
LANGUAGE = "fr-FR"

#: How much of a summary is worth giving a search engine as a description. Well
#: under what any of them display, and enough to be a sentence rather than a cut.
_DESCRIPTION_CHARS = 300


def _trimmed(text: str | None, limit: int = _DESCRIPTION_CHARS) -> str:
    """The first ``limit`` characters of a block, cut on a word."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0] + "…"


def _pruned(node: dict[str, Any]) -> dict[str, Any]:
    """Drop the keys with nothing behind them.

    A law written by hand has no promulgation date and may have no official
    reference; a `"legislationDate": null` is worse than an absent one, because
    it states that the value is known to be nothing.
    """
    return {key: value for key, value in node.items() if value not in (None, "", [], {})}


def website(origin: str, *, description: str) -> dict:
    """The site itself, and where to send a query typed at a search engine.

    The ``SearchAction`` names `/recherche` as the place a search goes. That page
    is `noindex` — deliberately, it is an unbounded space of thin pages — and the
    two are not in tension: this describes where to send a *person*, not a page
    to rank.
    """
    return {
        "@type": "WebSite",
        "@id": f"{origin}/#site",
        "url": f"{origin}/",
        "name": SITE_NAME,
        "description": description,
        "inLanguage": LANGUAGE,
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{origin}/recherche?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }


def breadcrumb(origin: str, trail: list[tuple[str, str]]) -> dict:
    """A trail of (label, path) pairs, as the crumb line shown under a result."""
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": n, "name": label, "item": f"{origin}{path}"}
            for n, (label, path) in enumerate(trail, start=1)
        ],
    }


def legislation(origin: str, law: Law) -> dict:
    """One law, as the thing it is rather than as a page that mentions it.

    `Legislation` is a real schema.org type with properties that map onto exactly
    what the Journal Officiel publishes and what the staged import already
    stores — the number, the day of promulgation, the jurisdiction, the chamber
    that passed it. This site had all of it and was saying none of it.

    ``sameAs`` points at the official text on Légifrance: the same law, published
    by the authority that publishes laws. It is the single most useful statement
    in this block, because it ties a page nobody has heard of to a document
    everybody trusts.

    It goes through :func:`~app.security.safe_url` first, and that is not
    belt-and-braces — the first version of this function did not, and
    `test_a_stored_javascript_url_still_never_renders` failed on it immediately.
    The template renders every href through the `safe_href` filter precisely
    because a row can predate the admin form's check; a second place that reads
    the same column has to make the same check, or the sanitising becomes a
    property of one template rather than of the value.
    """
    return _pruned(
        {
            "@type": "Legislation",
            "@id": f"{origin}/loi/{law.slug}#loi",
            "url": f"{origin}/loi/{law.slug}",
            "name": law.title,
            "description": _trimmed(law.summary_text),
            "legislationIdentifier": law.official_ref,
            "legislationDate": law.promulgated_on.isoformat() if law.promulgated_on else None,
            "legislationJurisdiction": "France",
            "legislationPassedBy": {"@type": "Organization", "name": "Parlement français"},
            "inLanguage": LANGUAGE,
            "sameAs": safe_url(law.legifrance_url),
        }
    )


def legislation_ref(origin: str, law: Law) -> dict:
    """The same law, named only — for a page that talks *about* it.

    The results page shows a votation's figures, not the law's presentation: its
    title is there, and a link, and nothing else. Emitting the full node from
    :func:`legislation` there put « Ce que dit la loi » into the markup of a page
    that does not display it, which breaks the rule this module opens with and is
    what search engines mean by hidden content.

    It broke something sharper too, and that is how it was caught rather than
    reasoned about: the summary is also the newest value in the modification
    history, so page *two* of that history suddenly carried page one's content —
    `test_a_long_history_is_paginated_rather_than_shipped_whole` failed on the
    spot. A public page had started shipping what pagination exists to withhold.

    What is left is an identity: enough for the dataset's ``about`` to resolve
    inside the document, and every word of it visible on the page.
    """
    return {
        "@type": "Legislation",
        "@id": f"{origin}/loi/{law.slug}#loi",
        "url": f"{origin}/loi/{law.slug}",
        "name": law.title,
    }


def votation_dataset(origin: str, law: Law) -> dict:
    """The open-data export of one votation, as a dataset.

    The charter's promise is that the raw figures are downloadable and checkable
    by anybody; this is that promise written where a machine looking for datasets
    can find it. The two distributions are the same two files the page offers.

    No `license` — see the module docstring. `isAccessibleForFree` is a fact
    about this deployment and is safe to state; a licence is a decision.
    """
    page = f"{origin}/loi/{law.slug}/resultats"
    return _pruned(
        {
            "@type": "Dataset",
            "@id": f"{page}#donnees",
            "url": page,
            "name": f"Votation citoyenne sur « {law.title} » — résultats bruts",
            "description": (
                "Résultats anonymisés de la votation citoyenne organisée sur cette loi : "
                "une ligne par vote, sans aucun identifiant de votant·e. "
                "L'horodatage est arrondi, et omis en dessous du seuil d'anonymat."
            ),
            "creator": {"@type": "Organization", "name": SITE_NAME, "url": f"{origin}/"},
            "isAccessibleForFree": True,
            "inLanguage": LANGUAGE,
            "about": {"@id": f"{origin}/loi/{law.slug}#loi"},
            "temporalCoverage": _coverage(law),
            "distribution": [
                {
                    "@type": "DataDownload",
                    "encodingFormat": "text/csv",
                    "contentUrl": f"{origin}/loi/{law.slug}/export.csv",
                },
                {
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": f"{origin}/loi/{law.slug}/export.json",
                },
            ],
        }
    )


def _coverage(law: Law) -> str:
    """The voting window as an ISO 8601 interval, or "" if it is not bounded.

    Schema.org's open-ended form is a trailing slash — « opened on this day and
    still running » — which is exactly what an open votation is.
    """
    if not law.opens_at:
        return ""
    start = law.opens_at.date().isoformat()
    return f"{start}/{law.closes_at.date().isoformat()}" if law.closes_at else f"{start}/"


def graph(*nodes: dict) -> dict:
    """The nodes of one page, in the single block that page emits.

    One `<script>` per page rather than one per node: the nodes reference each
    other by `@id` (a dataset is `about` a law), and a search engine resolves
    those references within a document.
    """
    return {"@context": "https://schema.org", "@graph": list(nodes)}
