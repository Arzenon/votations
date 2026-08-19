"""Discover newly promulgated laws from the Assemblée Nationale open data.

Downloads the ``Dossiers_Legislatifs`` export (Licence Ouverte, no
authentication) and extracts every dossier that reached promulgation — i.e. the
text is now a law of the Republic, with an official number and a Journal
Officiel reference.

Design notes:
- The HTTP getter is injectable so the parsing is unit-testable offline.
- Any failure raises :class:`AssembleeFetchError`; callers leave the cache intact.
- The export is a mechanical XML→JSON conversion, so single-element collections
  collapse from a list to a bare object. Every collection is normalised through
  :func:`_as_list` and the acts are walked recursively — never indexed.
- The legislature number is part of the export URL and changes once per
  legislature, which is why it lives in settings rather than in this module.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from app.config import get_settings

# Entries of interest inside the archive; the export also ships a `json/document/`
# tree of bill/report notices, which carries no promulgation information.
DOSSIER_PREFIX = "json/dossierParlementaire/"

# The act that records promulgation and carries the law number and JO reference.
# A sibling "PROM" act exists but its dateActe is always null.
PROMULGATION_ACTE = "PROM-PUB"

# Acts recording a sitting where the text was discussed. Each carries the sitting
# (reunionRef) and the agenda item within it (odjRef) — together they identify
# which part of which compte rendu concerns this law.
SEANCE_ACTE = "AN1-DEBATS-SEANCE"

AN_DOSSIER_URL_TEMPLATE = "https://www.assemblee-nationale.fr/dyn/{legislature}/dossiers/{uid}"


class AssembleeFetchError(RuntimeError):
    """Raised on any network/HTTP/archive/parse problem so callers keep old rows."""


@dataclass
class PromulgatedLaw:
    """One promulgated law, as published by the Assemblée Nationale."""

    dossier_uid: str
    legislature: str
    title: str            # titreDossier.titre — the readable dossier title
    law_title: str        # titreLoi — a fragment ("visant à …")
    law_code: str         # "2026-532"
    procedure: str        # procedureParlementaire.libelle
    promulgated_on: date
    jo_date: date | None
    jo_number: str | None
    nor: str | None       # NOR reference — the join key to Légifrance
    legifrance_url: str | None
    dossier_url: str
    # [{"reunion_ref": ..., "odj_ref": ...}] — the sittings where the text was
    # debated, in chronological order. Empty for a text adopted without debate.
    seances: list[dict] = field(default_factory=list)


Getter = Callable[[str], bytes]


def _default_getter(url: str) -> bytes:
    # A generous timeout: the archive is ~10 MB and served from a public portal.
    try:
        resp = httpx.get(url, timeout=120.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as exc:
        raise AssembleeFetchError(str(exc)) from exc


def _as_list(node: object) -> list:
    """Normalise an XML→JSON collection that may have collapsed to one object."""
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def _iter_actes(node: object) -> Iterator[dict]:
    """Yield every acte in the tree, at any nesting depth."""
    for acte in _as_list(node):
        if not isinstance(acte, dict):
            continue
        yield acte
        # `actesLegislatifs` may be present but null, so coalesce before indexing.
        yield from _iter_actes((acte.get("actesLegislatifs") or {}).get("acteLegislatif"))


def _seances(dossier: dict) -> list[dict]:
    """Sittings where the text was discussed, oldest first, without duplicates."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    actes = dossier.get("actesLegislatifs") or {}
    for acte in _iter_actes(actes.get("acteLegislatif")):
        if acte.get("codeActe") != SEANCE_ACTE:
            continue
        reunion, odj = acte.get("reunionRef"), acte.get("odjRef")
        if not (reunion and odj) or (reunion, odj) in seen:
            continue
        seen.add((reunion, odj))
        out.append({"reunion_ref": reunion, "odj_ref": odj, "date": acte.get("dateActe") or ""})
    out.sort(key=lambda s: s["date"])
    return out


def _find_promulgation(dossier: dict) -> dict | None:
    actes = dossier.get("actesLegislatifs") or {}
    for acte in _iter_actes(actes.get("acteLegislatif")):
        if acte.get("codeActe") == PROMULGATION_ACTE:
            return acte
    return None


def _parse_date(value: str | None) -> date | None:
    """Accept both the datetime form and the offset-only date form of the export."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _build(dossier: dict, acte: dict) -> PromulgatedLaw | None:
    """Map one dossier + its promulgation acte, or None if unusable."""
    uid = dossier.get("uid")
    law_code = acte.get("codeLoi")
    promulgated_on = _parse_date(acte.get("dateActe"))
    if not (uid and law_code and promulgated_on):
        return None

    legislature = str(dossier.get("legislature") or "")
    info_jo = acte.get("infoJO") or {}
    return PromulgatedLaw(
        dossier_uid=uid,
        legislature=legislature,
        title=(dossier.get("titreDossier") or {}).get("titre") or acte.get("titreLoi") or uid,
        law_title=acte.get("titreLoi") or "",
        law_code=law_code,
        procedure=(dossier.get("procedureParlementaire") or {}).get("libelle") or "",
        promulgated_on=promulgated_on,
        jo_date=_parse_date(info_jo.get("dateJO")),
        jo_number=info_jo.get("numJO"),
        nor=info_jo.get("referenceNOR"),
        legifrance_url=info_jo.get("urlLegifrance"),
        dossier_url=AN_DOSSIER_URL_TEMPLATE.format(legislature=legislature, uid=uid),
        seances=_seances(dossier),
    )


def fetch_promulgated_laws(get: Getter | None = None) -> list[PromulgatedLaw]:
    """Fetch every promulgated law from the export, newest first.

    Raises AssembleeFetchError on network, archive or whole-export parse failure.
    """
    settings = get_settings()
    get = get or _default_getter
    blob = get(settings.an_dossiers_url)

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        names = [
            n for n in archive.namelist()
            if n.startswith(DOSSIER_PREFIX) and n.endswith(".json")
        ]
    except zipfile.BadZipFile as exc:
        raise AssembleeFetchError(f"archive illisible : {exc}") from exc
    if not names:
        raise AssembleeFetchError("aucun dossier parlementaire dans l'archive")

    since = _parse_date(settings.an_import_since)
    laws: list[PromulgatedLaw] = []
    for name in names:
        try:
            dossier = json.loads(archive.read(name))["dossierParlementaire"]
        except (KeyError, TypeError, ValueError, zipfile.BadZipFile):
            continue  # one malformed dossier must not abort the whole import
        acte = _find_promulgation(dossier)
        if acte is None:
            continue
        law = _build(dossier, acte)
        if law is None:
            continue
        if since and law.promulgated_on < since:
            continue
        laws.append(law)

    laws.sort(key=lambda law: (law.promulgated_on, law.law_code), reverse=True)
    return laws
