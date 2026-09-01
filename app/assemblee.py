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
  :func:`~app.opendata.as_list` and the acts are walked recursively — never indexed.
- The legislature number is part of the export URL and changes once per
  legislature, which is why it lives in settings rather than in this module.
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

# The largest single dossier worth reading, uncompressed. Real ones are tens of
# kilobytes; a `zipfile` member declares its own uncompressed size, and reading
# one without looking first is how a few kilobytes of archive become gigabytes of
# memory. The same rule the Journal Officiel reader already applies to its tar
# members — see `app.legifrance.MAX_MEMBER_BYTES`.
MAX_DOSSIER_BYTES = 32 * 1024 * 1024


class AssembleeFetchError(RuntimeError):
    """Raised on any network/HTTP/archive/parse problem so callers keep old rows."""


@dataclass
class PromulgatedLaw:
    """One promulgated law, as published by the Assemblée Nationale."""

    dossier_uid: str
    legislature: str
    title: str  # titreDossier.titre — the readable dossier title
    law_code: str  # "2026-532"
    procedure: str  # procedureParlementaire.libelle
    promulgated_on: date
    jo_date: date | None
    jo_number: str | None
    nor: str | None  # NOR reference — the join key to Légifrance
    legifrance_url: str | None
    dossier_url: str
    # [{"reunion_ref": ..., "odj_ref": ...}] — the sittings where the text was
    # debated, in chronological order. Empty for a text adopted without debate.
    seances: list[dict] = field(default_factory=list)


# A generous timeout: the archive is ~10 MB and served from a public portal.
#
# The size ceiling exists for the same reason as the per-dossier one above — the
# download used to be the one step with nothing bounding it — and is sized against
# the container rather than only against the source. A download peaks at roughly
# twice this figure (see `app.opendata.fetch`), and the blob is copied again into
# a `BytesIO` below, so the job holds ~3× the cap at its worst moment inside a
# 1 GB limit. At 256 MB, which is what this used to be, the ceiling was set above
# the point where the job is killed for reaching it, which makes it no ceiling at
# all. Six times the largest real export is ample.
MAX_EXPORT_BYTES = 64 * 1024 * 1024
_default_getter = getter(timeout=120.0, max_bytes=MAX_EXPORT_BYTES, error=AssembleeFetchError)


def _iter_actes(node: object) -> Iterator[dict]:
    """Yield every acte in the tree, at any nesting depth."""
    for acte in as_list(node):
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


def _import_floor(settings) -> date | None:
    """Oldest promulgation the import will accept.

    A rolling window by default (``AN_IMPORT_SINCE_DAYS``) so the job does not
    quietly accumulate a growing backlog the way a hard-coded date does; set
    ``AN_IMPORT_SINCE`` to pin an absolute date, or clear both to take everything.
    """
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

    # Closed on the way out: the archive holds the whole 10 MB blob open, and this
    # runs in a container with a memory limit precisely because it does.
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
                    # Read the declared size, not the member: no real dossier is
                    # this large, so this is a malformed or hostile archive.
                    continue
                # One malformed dossier must not abort the whole import — and
                # until this covered the *reading* as well as the decoding, it
                # did not say so. `json.loads` was inside and `_build` was not,
                # while `_build` is where the shapes actually vary: this is a
                # mechanical XML→JSON conversion, so a collection holding one
                # element arrives as a bare object (which is why `as_list` exists)
                # and a field the source writes as text can arrive as a mapping.
                # `_parse_date` on a dict raises TypeError, `.get` on a list
                # raises AttributeError, and either one threw away the other
                # three thousand dossiers on its way out.
                #
                # `RecursionError` for the same reason one line down: `json.loads`
                # raises it, not ValueError, on a deeply nested document.
                try:
                    dossier = json.loads(archive.read(info))["dossierParlementaire"]
                    acte = _find_promulgation(dossier)
                    if acte is None:
                        continue
                    law = _build(dossier, acte)
                except (
                    AttributeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    RecursionError,
                    zipfile.BadZipFile,
                ):
                    log.warning("dossier ignoré : %s — structure inattendue", info.filename)
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
