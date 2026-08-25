"""Attributed speeches from Assemblée nationale sitting records.

Given the sittings recorded on a dossier, fetch the comptes rendus and return the
speeches where positions are actually stated — presentation, motions, discussion
générale, explications de vote — each carrying the speaker's name and a link back
to the record.

This exists to source the two argument blocks of a law's presentation. The
charter requires arguments to be attributed, so a speech with no identifiable
speaker is dropped rather than kept anonymously.

Design notes:
- The HTTP getter is injectable so the parsing is unit-testable offline.
- Two small fetches per sitting: the réunion (~2 kB) names its compte rendu and
  its agenda, then the compte rendu (~600 kB) carries the speeches. No corpus
  download and nothing stored.
- A sitting can debate several bills. The dossier's ``odj_ref`` identifies which
  agenda item is ours, and its position in the réunion's agenda equals the
  ``valeur_ptsodj`` the compte rendu tags each paragraph with — so speeches about
  another bill are never mixed in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from app.opendata import Getter, as_list, getter

OPENDATA_URL = "https://www.assemblee-nationale.fr/dyn/opendata/{uid}.json"
SEANCE_URL = "https://www.assemblee-nationale.fr/dyn/{legislature}/comptes-rendus/seance/{uid}"

# Agenda-item sections where members argue for or against the text as a whole.
# Everything else — amendment-by-amendment discussion, points of order, voting
# mechanics — is high volume and low argumentative content.
ARGUMENT_SECTIONS = (
    "PRESENTATION",  # rapporteur and government presenting the text
    "MOTION",  # motion de rejet préalable / renvoi — argued against
    "DISC_GENERALE",  # discussion générale
    "DISC_ARTICLES_1_2",  # explications de vote (despite the name)
)

# Paragraph kinds to drop even inside those sections.
SKIP_GRAMMAR = ("INTERRUPTION", "SUSP_SEANCE", "SCRUT", "ANN_SCR", "VOTE_")

# Chair interventions are procedure, not argument.
SKIP_ROLES = ("president",)

# Below this, a "speech" is an interjection rather than an argument.
MIN_SPEECH_CHARS = 200


class DebatesFetchError(RuntimeError):
    """Raised on network or parse failure; callers fall back to no arguments."""


@dataclass
class Speech:
    """One attributed intervention, quotable and linkable."""

    speech_id: str  # stable within a request, referenced by the model
    speaker: str  # "Mme Louise Morel"
    quality: str  # "ministre délégué…", "rapporteure", "" for a deputy
    chamber: str  # "Assemblée nationale"
    section: str  # "Discussion générale"
    spoken_on: date | None
    text: str
    url: str

    @property
    def attribution(self) -> str:
        """The author line stored on the argument, built from our data only."""
        who = f"{self.speaker} ({self.quality})" if self.quality else self.speaker
        when = f", séance du {self.spoken_on:%d/%m/%Y}" if self.spoken_on else ""
        return f"{who}{when} · {self.chamber}"


_default_getter = getter(timeout=60.0, error=DebatesFetchError)


def _as_text(node: object) -> str:
    """`texte` is a string on paragraphs but an empty dict on some points."""
    return node.strip() if isinstance(node, str) else ""


# What an Assemblée nationale identifier looks like: "RUANR5L17S2026PO861108N4",
# "CRSANR5L17S2026IDS31234". Letters, digits, and nothing that means anything to
# a URL.
_UID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _get_json(get: Getter, uid: str) -> dict:
    """Fetch and decode one open-data document, by identifier.

    The identifier is checked against what an identifier can look like before it
    is put in a URL. It arrives from the government export by way of our own
    staging table, so this is not a suspicion about the Assemblée nationale — it
    is that a value which reaches an outbound request should be a value we
    recognise, whatever the path it took to get here. A `../` in this position
    would aim the fetch at another document on that host.
    """
    if not _UID_RE.match(uid):
        raise DebatesFetchError(f"identifiant de séance inattendu : {uid!r}")
    try:
        return json.loads(get(OPENDATA_URL.format(uid=uid)))
    except ValueError as exc:
        raise DebatesFetchError(f"réponse illisible pour {uid} : {exc}") from exc


def _agenda_position(reunion: dict, odj_ref: str) -> int | None:
    """1-based index of our agenda item, which the compte rendu calls valeur_ptsodj."""
    points = as_list(((reunion.get("ODJ") or {}).get("pointsODJ") or {}).get("pointODJ"))
    for index, point in enumerate(points, start=1):
        if isinstance(point, dict) and point.get("uid") == odj_ref:
            return index
    return None


def _seance_date(compte_rendu: dict) -> date | None:
    raw = str((compte_rendu.get("metadonnees") or {}).get("dateSeance") or "")
    if len(raw) < 8:
        return None
    try:
        # A calendar date with no time of day: the zone would be meaningless.
        return datetime.strptime(raw[:8], "%Y%m%d").date()  # noqa: DTZ007
    except ValueError:
        return None


def _speech_sections(compte_rendu: dict) -> dict[str, str]:
    """Agenda position → the argumentative section title found under it."""
    titles: dict[str, str] = {}

    def walk(point: dict) -> None:
        attrs = point.get("@attributes") or {}
        code = attrs.get("code_grammaire") or ""
        ptsodj = str(attrs.get("valeur_ptsodj") or "")
        text = _as_text(point.get("texte"))
        if text and code.startswith(ARGUMENT_SECTIONS):
            titles.setdefault(ptsodj, text)
        for sub in as_list(point.get("point")):
            if isinstance(sub, dict):
                walk(sub)

    for point in as_list((compte_rendu.get("contenu") or {}).get("point")):
        if isinstance(point, dict):
            walk(point)
    return titles


def _collect(node: object, out: list[dict], section_stack: list[str]) -> None:
    """Walk the record, tracking the innermost argumentative section title."""
    if isinstance(node, list):
        for item in node:
            _collect(item, out, section_stack)
        return
    if not isinstance(node, dict):
        return

    attrs = node.get("@attributes") or {}
    code = attrs.get("code_grammaire") or ""
    pushed = False
    text = _as_text(node.get("texte"))

    # A point that opens an argumentative section names it for everything below.
    if text and code.startswith(ARGUMENT_SECTIONS):
        section_stack.append(text)
        pushed = True

    if "orateurs" in node and isinstance(node.get("texte"), str):
        out.append(
            {
                "attrs": attrs,
                "orateurs": node["orateurs"],
                "texte": node["texte"],
                "section": section_stack[-1] if section_stack else "",
            }
        )

    for key, value in node.items():
        if key not in ("@attributes", "orateurs", "texte"):
            _collect(value, out, section_stack)

    if pushed:
        section_stack.pop()


def _speaker(orateurs: object) -> tuple[str, str]:
    orateur = (orateurs or {}).get("orateur") if isinstance(orateurs, dict) else None
    if isinstance(orateur, list):
        orateur = orateur[0] if orateur else None
    if not isinstance(orateur, dict):
        return "", ""
    name = (orateur.get("nom") or "").strip()
    qualite = orateur.get("qualite")
    quality = qualite.strip() if isinstance(qualite, str) else ""
    return name, quality


def _clean(text: str) -> str:
    # The records carry typographic markup like "n[[o]]" for the ordinal sign.
    text = text.replace("[[o]]", "°")
    return re.sub(r"\s+", " ", text).strip()


def speeches_from_record(compte_rendu: dict, *, position: int | None, url: str) -> list[Speech]:
    """Extract the argued positions for one agenda item of one sitting."""
    sections = _speech_sections(compte_rendu)
    # With a single argued item, the position tag is not needed to disambiguate.
    if position is None and len(sections) != 1:
        return []
    wanted = str(position) if position is not None else next(iter(sections))
    if wanted not in sections:
        return []

    spoken_on = _seance_date(compte_rendu)
    uid = compte_rendu.get("uid") or ""
    raw: list[dict] = []
    _collect((compte_rendu.get("contenu") or {}).get("point"), raw, [])

    out: list[Speech] = []
    for index, item in enumerate(raw):
        attrs = item["attrs"]
        if str(attrs.get("valeur_ptsodj") or "") != wanted:
            continue
        code = attrs.get("code_grammaire") or ""
        if code.startswith(SKIP_GRAMMAR):
            continue
        if (attrs.get("roledebat") or "") in SKIP_ROLES:
            continue
        if not item["section"]:
            continue
        speaker, quality = _speaker(item["orateurs"])
        if not speaker or speaker.lower().endswith("le président"):
            continue  # unattributable, or the chair running the sitting
        text = _clean(item["texte"])
        if len(text) < MIN_SPEECH_CHARS:
            continue
        out.append(
            Speech(
                speech_id=f"{uid}-{index}",
                speaker=speaker,
                quality=quality,
                chamber="Assemblée nationale",
                section=item["section"],
                spoken_on=spoken_on,
                text=text,
                url=url,
            )
        )
    return out


def fetch_speeches(seances: list[dict], get: Getter | None = None) -> list[Speech]:
    """Fetch every argued speech about this text, across its sittings.

    A sitting that cannot be read is skipped rather than fatal — a partial set of
    arguments is useful, and the admin sees how many sittings were covered.
    """
    get = get or _default_getter
    out: list[Speech] = []
    for seance in seances:
        reunion_ref = (seance or {}).get("reunion_ref")
        odj_ref = (seance or {}).get("odj_ref")
        if not reunion_ref:
            continue
        try:
            reunion = _get_json(get, reunion_ref)
            cr_ref = reunion.get("compteRenduRef")
            if not cr_ref:
                continue
            position = _agenda_position(reunion, odj_ref) if odj_ref else None
            record = _get_json(get, cr_ref)
        except DebatesFetchError:
            continue
        legislature = (record.get("metadonnees") or {}).get("legislature") or "17"
        url = SEANCE_URL.format(legislature=legislature, uid=cr_ref)
        out.extend(speeches_from_record(record, position=position, url=url))
    return out
