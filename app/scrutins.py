"""Assemblée nationale vote counts for a law.

Read from the scrutins export, joined on the vote reference of the dossier's
decision acts. Assemblée only: the Sénat publishes its votes elsewhere.

Failures raise `ScrutinsFetchError`; the import carries on without counts.
The archive has about 8,400 scrutins and we need about 150, so members are
read by name.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass

from app.config import get_settings
from app.logging_config import get_logger
from app.opendata import Getter, getter

log = get_logger("scrutins")

MEMBER = "json/{uid}.json"

# The export is about 25 MB.
MAX_EXPORT_BYTES = 64 * 1024 * 1024

# Max uncompressed size of one scrutin (real ones are a few kB). Zip bomb guard.
MAX_MEMBER_BYTES = 16 * 1024 * 1024


class ScrutinsFetchError(RuntimeError):
    """Network, archive or parse failure."""


_default_getter = getter(timeout=120.0, max_bytes=MAX_EXPORT_BYTES, error=ScrutinsFetchError)


@dataclass(frozen=True)
class Scrutin:
    """One recorded vote."""

    uid: str
    # "adopté" or "rejeté"
    sort: str
    # e.g. "l'ensemble du projet de loi...", "la première partie..."
    objet: str
    pour: int
    contre: int
    abstentions: int


def _count(decompte: dict, key: str) -> int:
    """A vote count as an int (the export uses strings)."""
    try:
        return int(decompte.get(key) or 0)
    except TypeError, ValueError:
        return 0


def _parse(document: dict) -> Scrutin | None:
    scrutin = document.get("scrutin") if isinstance(document, dict) else None
    if not isinstance(scrutin, dict) or not scrutin.get("uid"):
        return None
    decompte = ((scrutin.get("syntheseVote") or {}).get("decompte")) or {}
    return Scrutin(
        uid=str(scrutin["uid"]),
        sort=str((scrutin.get("sort") or {}).get("code") or ""),
        objet=str((scrutin.get("objet") or {}).get("libelle") or ""),
        pour=_count(decompte, "pour"),
        contre=_count(decompte, "contre"),
        abstentions=_count(decompte, "abstentions"),
    )


def fetch_votes(wanted: set[str], get: Getter | None = None) -> dict[str, Scrutin]:
    """The scrutins listed in ``wanted``. Missing or unreadable ones are left out."""
    if not wanted:
        return {}
    get = get or _default_getter
    blob = get(get_settings().an_scrutins_url)

    found: dict[str, Scrutin] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            # infolist() rather than namelist(), to check sizes before reading.
            available = {info.filename: info for info in archive.infolist()}
            for uid in wanted:
                info = available.get(MEMBER.format(uid=uid))
                if info is None:
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    log.warning("scrutin %s anormalement volumineux - ignoré", uid)
                    continue
                try:
                    scrutin = _parse(json.loads(archive.read(info)))
                except ValueError, RecursionError, KeyError, TypeError, zipfile.BadZipFile:
                    log.warning("scrutin %s illisible - ignoré", uid)
                    continue
                if scrutin is not None:
                    found[scrutin.uid] = scrutin
    except zipfile.BadZipFile as exc:
        raise ScrutinsFetchError(f"archive illisible : {exc}") from exc
    return found


# A vote on part of a text (e.g. "la première partie" of a loi de finances)
# isn't a vote on the whole text. Some read "l'ensemble de la première partie",
# so checking for "l'ensemble" isn't enough. The ordinal is required: "partie"
# alone also matches "partie législative du code", "États parties", "contrepartie".
_A_PART_OF_THE_TEXT = re.compile(r"\b(premi[eè]re|deuxi[eè]me|seconde|troisi[eè]me) partie\b")

# Only adopted votes count. Also guards against motions de rejet, where "pour"
# means rejecting the text (already excluded in `app.assemblee`).
_ADOPTED = "adopté"


def final_vote(vote_refs: list[str], index: dict[str, Scrutin]) -> Scrutin | None:
    """The last vote that adopted the whole text.

    ``vote_refs`` is oldest first across all readings, and the final adoption
    is the one that counts. Looking at every reading finds a count for 94 of
    147 laws, against 73 for the first reading only.
    """
    chosen: Scrutin | None = None
    for ref in vote_refs:
        scrutin = index.get(ref)
        if scrutin is None or scrutin.sort != _ADOPTED:
            continue
        if _A_PART_OF_THE_TEXT.search(scrutin.objet.lower()):
            continue
        chosen = scrutin
    return chosen
