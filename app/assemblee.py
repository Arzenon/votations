"""Find newly promulgated laws in the Assemblée nationale open data.

Downloads the ``Dossiers_Legislatifs`` export and keeps the dossiers that
reached promulgation. Any failure raises `AssembleeFetchError` and existing
rows are kept. The export is converted from XML, so collections go through
`as_list` and acts are walked recursively.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.config import get_settings
from app.logging_config import get_logger
from app.opendata import Getter, as_list, getter

log = get_logger("assemblee")

# The archive also has a json/document/ tree, not needed here.
DOSSIER_PREFIX = "json/dossierParlementaire/"

# Promulgation act, with the law number and JO reference. (The "PROM" act
# always has a null dateActe.)
PROMULGATION_ACTE = "PROM-PUB"

# Decision acts, whose votes are on the text itself. Matching the suffix
# excludes "-MOTION-VOTE" acts, where "pour" means voting to reject the text.
DECISION_SUFFIX = "-DEC"

# A commission mixte paritaire was convened (the chambers disagreed).
CMP_ACTE = "CMP"

# Sitting where the text was discussed: reunionRef + odjRef identify the part
# of the compte rendu about this law.
SEANCE_ACTE = "AN1-DEBATS-SEANCE"

AN_DOSSIER_URL_TEMPLATE = "https://www.assemblee-nationale.fr/dyn/{legislature}/dossiers/{uid}"

# Max uncompressed size of a dossier (real ones are tens of kB). Protects
# against zip bombs.
MAX_DOSSIER_BYTES = 32 * 1024 * 1024


class AssembleeFetchError(RuntimeError):
    """Network, archive or parse failure. Callers keep the existing rows."""


@dataclass
class PromulgatedLaw:
    """One promulgated law, as published by the Assemblée Nationale."""

    dossier_uid: str
    legislature: str
    title: str  # titreDossier.titre
    law_code: str  # "2026-532"
    procedure: str  # procedureParlementaire.libelle
    promulgated_on: date
    jo_date: date | None
    jo_number: str | None
    nor: str | None  # key to Légifrance
    legifrance_url: str | None
    dossier_url: str
    # [{"reunion_ref": ..., "odj_ref": ...}], oldest first. Empty if no debate.
    seances: list[dict] = field(default_factory=list)
    # Votes referenced by decision acts, oldest first. Not stored:
    # `app.scrutins.final_vote` uses them to find the final vote.
    vote_refs: list[str] = field(default_factory=list)
    cmp_convened: bool = False
    # Final vote on the whole text, from the scrutins export. None when there
    # was no public scrutin.
    an_vote_for: int | None = None
    an_vote_against: int | None = None
    an_vote_abstain: int | None = None
    # False if the scrutins download failed, so we don't claim there was no vote.
    an_vote_checked: bool = False


# The archive is about 10 MB. The job can use about 3x this cap in memory
# (see `app.opendata.fetch`), within the container's 1 GB limit.
MAX_EXPORT_BYTES = 64 * 1024 * 1024
_default_getter = getter(timeout=120.0, max_bytes=MAX_EXPORT_BYTES, error=AssembleeFetchError)


def _iter_actes(node: object) -> Iterator[dict]:
    """Yield every acte in the tree, at any nesting depth."""
    for acte in as_list(node):
        if not isinstance(acte, dict):
            continue
        yield acte
        # actesLegislatifs can be null.
        yield from _iter_actes((acte.get("actesLegislatifs") or {}).get("acteLegislatif"))


@dataclass
class _Actes:
    """What we read from a dossier's acts, collected in a single pass."""

    # The PROM-PUB act; None if the dossier never became law.
    promulgation: dict | None = None
    # Sittings, oldest first, deduplicated.
    seances: list[dict] = field(default_factory=list)
    # Votes from "-DEC" acts only (see DECISION_SUFFIX), in chronological order.
    vote_refs: list[str] = field(default_factory=list)
    cmp_convened: bool = False


def _read_actes(dossier: dict) -> _Actes:
    """Walk the dossier's acts once."""
    out = _Actes()
    seen: set[tuple[str, str]] = set()
    root = (dossier.get("actesLegislatifs") or {}).get("acteLegislatif")

    for acte in _iter_actes(root):
        code = acte.get("codeActe") or ""
        # The first PROM-PUB wins.
        if code == PROMULGATION_ACTE and out.promulgation is None:
            out.promulgation = acte
        elif code == CMP_ACTE:
            out.cmp_convened = True
        elif code == SEANCE_ACTE:
            reunion, odj = acte.get("reunionRef"), acte.get("odjRef")
            if reunion and odj and (reunion, odj) not in seen:
                seen.add((reunion, odj))
                out.seances.append(
                    {"reunion_ref": reunion, "odj_ref": odj, "date": acte.get("dateActe") or ""}
                )
        if code.endswith(DECISION_SUFFIX):
            for ref in as_list((acte.get("voteRefs") or {}).get("voteRef")):
                if isinstance(ref, str) and ref:
                    out.vote_refs.append(ref)

    out.seances.sort(key=lambda s: s["date"])
    return out


def _parse_date(value: str | None) -> date | None:
    """Parse the export's dates (with or without a time)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _build(dossier: dict, actes: _Actes) -> PromulgatedLaw | None:
    """Build a law from a dossier, or None if not promulgated or incomplete."""
    acte = actes.promulgation
    if acte is None:
        return None
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
        law_code=law_code,
        procedure=(dossier.get("procedureParlementaire") or {}).get("libelle") or "",
        promulgated_on=promulgated_on,
        jo_date=_parse_date(info_jo.get("dateJO")),
        jo_number=info_jo.get("numJO"),
        nor=info_jo.get("referenceNOR"),
        legifrance_url=info_jo.get("urlLegifrance"),
        dossier_url=AN_DOSSIER_URL_TEMPLATE.format(legislature=legislature, uid=uid),
        seances=actes.seances,
        vote_refs=actes.vote_refs,
        cmp_convened=actes.cmp_convened,
    )


def _import_floor(settings) -> date | None:
    """Oldest promulgation date imported (``AN_IMPORT_SINCE`` or
    ``AN_IMPORT_SINCE_DAYS``), or None for everything."""
    pinned = _parse_date(settings.an_import_since)
    if pinned:
        return pinned
    days = settings.an_import_since_days
    if days and days > 0:
        return (datetime.now(UTC) - timedelta(days=days)).date()
    return None


def fetch_promulgated_laws(get: Getter | None = None) -> list[PromulgatedLaw]:
    """Fetch every promulgated law from the export, newest first.

    Raises AssembleeFetchError on network, archive or whole-export parse failure.
    """
    settings = get_settings()
    get = get or _default_getter
    blob = get(settings.an_dossiers_url)

    since = _import_floor(settings)
    laws: list[PromulgatedLaw] = []

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = [
                info
                for info in archive.infolist()
                if info.filename.startswith(DOSSIER_PREFIX) and info.filename.endswith(".json")
            ]
            if not entries:
                raise AssembleeFetchError("aucun dossier parlementaire dans l'archive")

            for info in entries:
                if info.file_size > MAX_DOSSIER_BYTES:
                    # Malformed or hostile archive.
                    continue
                # Skip a malformed dossier rather than abort the import. Fields
                # can have unexpected types after the XML conversion.
                try:
                    dossier = json.loads(archive.read(info))["dossierParlementaire"]
                    law = _build(dossier, _read_actes(dossier))
                except (
                    AttributeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    RecursionError,
                    zipfile.BadZipFile,
                ):
                    log.warning("dossier ignoré : %s - structure inattendue", info.filename)
                    continue
                if law is None:
                    continue
                if since and law.promulgated_on < since:
                    continue
                laws.append(law)
    except zipfile.BadZipFile as exc:
        raise AssembleeFetchError(f"archive illisible : {exc}") from exc

    laws.sort(key=lambda law: (law.promulgated_on, law.law_code), reverse=True)
    return laws
