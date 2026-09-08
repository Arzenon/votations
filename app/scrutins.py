"""How the Assemblée nationale actually voted on a law.

« Adoptée par 357 voix contre 177 » and « adoptée par 269 voix contre 0 » do not
say the same thing to somebody about to give their own opinion, and until now the
site said neither. The counts are published as open data (Licence Ouverte, no
authentication) in a second export beside the dossiers one, joined on the vote
reference a dossier's decision act carries.

**Only the Assemblée.** The Sénat publishes its own scrutins elsewhere and they
are not in this file, so everything here — and everything the page says about it
— names one chamber.

Design notes:
- The HTTP getter is injectable so the parsing is unit-testable offline.
- Any failure raises :class:`ScrutinsFetchError`. The daily import catches it and
  carries on: a vote count is worth having and is not worth losing a law over.
- The archive holds one JSON document per scrutin, named by its identifier, and
  there are 8 434 of them for a legislature against roughly 150 that any law
  refers to. So the members are read **by name**, not walked: the download is
  what it is, the parsing is proportional to what is actually wanted.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

from app.config import get_settings
from app.logging_config import get_logger
from app.opendata import Getter, getter

log = get_logger("scrutins")

#: Where one scrutin lives inside the archive, by identifier.
MEMBER = "json/{uid}.json"

#: The export runs to ~25 MB. Generous against that, and sized against the
#: container the same way the dossiers cap is — a download peaks at roughly twice
#: this figure, see `app.opendata.fetch`.
MAX_EXPORT_BYTES = 64 * 1024 * 1024

#: The largest single scrutin worth reading, uncompressed. Real ones are a few
#: kilobytes; a `zipfile` member declares its own uncompressed size, and reading
#: one without looking first is how a few kilobytes of archive become gigabytes of
#: memory. The cap on the download bounds the *compressed* bytes and nothing else.
#:
#: The same rule the two other archive readers already state — see
#: `app.legifrance.MAX_MEMBER_BYTES` and `app.assemblee.MAX_DOSSIER_BYTES`. This
#: reader was the one that did not apply it, which is precisely the way a rule
#: held in three places stops being one.
MAX_MEMBER_BYTES = 16 * 1024 * 1024


class ScrutinsFetchError(RuntimeError):
    """Raised on any network/archive/parse problem, so callers keep old rows."""


_default_getter = getter(timeout=120.0, max_bytes=MAX_EXPORT_BYTES, error=ScrutinsFetchError)


@dataclass(frozen=True)
class Scrutin:
    """One recorded vote: what it was about, how it went, and by how much."""

    uid: str
    #: "adopté" | "rejeté".
    sort: str
    #: « l'ensemble du projet de loi… », « la première partie… ». Read, not shown.
    objet: str
    pour: int
    contre: int
    abstentions: int


def _count(decompte: dict, key: str) -> int:
    """One figure of a vote, as a number. The export writes them as strings."""
    try:
        return int(decompte.get(key) or 0)
    except (TypeError, ValueError):
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
    """The scrutins named by ``wanted``, read out of the export by identifier.

    Returns what could be read. A reference the export does not carry is simply
    absent from the result — the law then has no published count, which is a
    state the page has to render anyway (a text adopted without a public scrutin
    is in exactly the same position).
    """
    if not wanted:
        return {}
    get = get or _default_getter
    blob = get(get_settings().an_scrutins_url)

    found: dict[str, Scrutin] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            # The entries by name, with their declared sizes — `namelist()` gave
            # only the first half, and the size is what has to be looked at before
            # a member is read into memory.
            available = {info.filename: info for info in archive.infolist()}
            for uid in wanted:
                info = available.get(MEMBER.format(uid=uid))
                if info is None:
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    # No real scrutin is this large, so this is a malformed or
                    # hostile archive. Skipped rather than fatal: the other
                    # hundred and fifty votes are still worth having.
                    log.warning("scrutin %s anormalement volumineux - ignoré", uid)
                    continue
                try:
                    scrutin = _parse(json.loads(archive.read(info)))
                except (ValueError, RecursionError, KeyError, TypeError, zipfile.BadZipFile):
                    log.warning("scrutin %s illisible - ignoré", uid)
                    continue
                if scrutin is not None:
                    found[scrutin.uid] = scrutin
    except zipfile.BadZipFile as exc:
        raise ScrutinsFetchError(f"archive illisible : {exc}") from exc
    return found


#: A scrutin on part of a text is not a scrutin on the text.
#:
#: Three lois de finances carry one: the Assemblée rejected « la première partie »
#: in first reading and the law was adopted later in commission mixte paritaire.
#: The `sort` filter below already catches those three, and this is the second
#: net, because the failure it guards against is silent and specific — one of them
#: reads « **l'ensemble de** la première partie », so a check for « l'ensemble »
#: passes it straight through.
_A_PART_OF_THE_TEXT = "partie"

#: The only outcome worth reading a count from. It is also the guard against the
#: polarity trap in the other direction: a text can be disposed of by adopting a
#: *motion de rejet*, where « pour 190 » means a hundred and ninety votes to throw
#: it out. Those votes hang off `…-MOTION-VOTE` acts, which
#: `app.assemblee` never collects — see the note there. This is the backstop.
_ADOPTED = "adopté"


def final_vote(vote_refs: list[str], index: dict[str, Scrutin]) -> Scrutin | None:
    """The vote that adopted this law as a whole, out of everything it references.

    ``vote_refs`` arrives oldest first, across every reading — first reading,
    second, commission mixte paritaire, lecture définitive. The one that matters
    is the **last** that adopted the whole text: a law read three times has three
    recorded votes and only the final one says how the Assemblée settled it.

    Taking the first, or taking the one attached to the first reading, is the
    mistake this function exists to avoid. Measured over a whole legislature it is
    not a marginal difference: reading only the first-reading act found a usable
    count for 73 of 147 promulgated laws, and walking every reading finds 94.
    """
    chosen: Scrutin | None = None
    for ref in vote_refs:
        scrutin = index.get(ref)
        if scrutin is None or scrutin.sort != _ADOPTED:
            continue
        if _A_PART_OF_THE_TEXT in scrutin.objet.lower():
            continue
        chosen = scrutin
    return chosen
