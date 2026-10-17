"""schema.org structured data (JSON-LD) for each page.

Only states what the page already shows. No licence is declared, since none
has been chosen yet. JSON-LD blocks aren't executed, so the CSP doesn't need
to change. Rendered with Jinja's ``tojson``, which escapes ``<`` and ``>``.
"""

from __future__ import annotations

from typing import Any

from app.labels import local_day
from app.models import Law
from app.security import safe_url

SITE_NAME = "Votations Françaises"

LANGUAGE = "fr-FR"

_DESCRIPTION_CHARS = 300


def _trimmed(text: str | None, limit: int = _DESCRIPTION_CHARS) -> str:
    """Truncate to ``limit`` characters on a word boundary."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0] + "…"


def _pruned(node: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values rather than emit nulls."""
    return {key: value for key, value in node.items() if value not in (None, "", [], {})}


def website(origin: str, *, description: str) -> dict:
    """The site, with a SearchAction pointing to /recherche."""
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
    """Breadcrumb from (label, path) pairs."""
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": n, "name": label, "item": f"{origin}{path}"}
            for n, (label, path) in enumerate(trail, start=1)
        ],
    }


def legislation(origin: str, law: Law) -> dict:
    """A law as a schema.org `Legislation`, linked to Légifrance via ``sameAs``.

    The URL goes through `safe_url`, like every href in the templates.
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
    """A minimal reference to the law, for the results page.

    The results page doesn't show the summary, so it mustn't be in its markup.
    """
    return {
        "@type": "Legislation",
        "@id": f"{origin}/loi/{law.slug}#loi",
        "url": f"{origin}/loi/{law.slug}",
        "name": law.title,
    }


def votation_dataset(origin: str, law: Law) -> dict:
    """A votation's open data export as a `Dataset` (for Google Dataset Search).

    The export URLs are disallowed in robots.txt so crawlers don't download
    them. Dataset Search reads this markup and doesn't need to fetch the files.
    """
    page = f"{origin}/loi/{law.slug}/resultats"
    return _pruned(
        {
            "@type": "Dataset",
            "@id": f"{page}#donnees",
            "url": page,
            "name": f"Votation citoyenne sur « {law.title} » : résultats bruts",
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
    """Voting window as an ISO 8601 interval ("start/" if open-ended), or "".

    Dates go through `local_day` to get the Paris day, not the UTC day.
    """
    if not law.opens_at:
        return ""
    start = local_day(law.opens_at).isoformat()
    return f"{start}/{local_day(law.closes_at).isoformat()}" if law.closes_at else f"{start}/"


def graph(*nodes: dict) -> dict:
    """All of a page's nodes in one block, so @id references resolve."""
    return {"@context": "https://schema.org", "@graph": list(nodes)}
