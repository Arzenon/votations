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
- A sitting can debate several bills, so finding *ours* in the record is the
  whole difficulty. The dossier's ``odj_ref`` names our agenda item; the compte
  rendu tags every paragraph with a ``valeur_ptsodj``, and its ``sommaire`` says
  which subject each of those numbers covers. We match our agenda item to a
  record point **by subject**, and read only that point.

  It used to match by *position* — the rank of our item in the réunion's agenda,
  assumed equal to ``valeur_ptsodj``. That assumption is simply false, and
  silently so in both directions. The two numbering schemes disagree whenever the
  agenda carries something the record does not number the same way, which is
  most sittings: on 5 May 2026 the loi de programmation militaire was agenda item
  7 and record point 5, and the record's only argued section, number 4, was
  « Lutte contre les fraudes sociales et fiscales » — a different bill, which the
  old rule was one coincidence away from quoting. Matching on the subject drops
  that risk and finds the speeches the position missed.

- Some texts are debated **jointly** — « discussion générale commune » — and the
  record then covers several agenda items under one number and one title, so no
  subject matches ours. Those speeches are kept rather than lost, and every one
  of them is marked :attr:`Speech.joint_debate` so the draft can warn that the
  passages may concern a neighbouring text too. That is a deliberate trade: the
  alternative was a law with twenty sittings and no arguments at all.
"""

from __future__ import annotations

import json
import re
import unicodedata
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
    #: True when the record debated this text jointly with another, so the
    #: passage may be arguing about the neighbouring one. Never guessed: it is
    #: set only where the record itself says « commune ».
    joint_debate: bool = False

    @property
    def attribution(self) -> str:
        """The author line stored on the argument, built from our data only."""
        who = f"{self.speaker} ({self.quality})" if self.quality else self.speaker
        when = f", séance du {self.spoken_on:%d/%m/%Y}" if self.spoken_on else ""
        return f"{who}{when} · {self.chamber}"


# A compte rendu runs to ~600 kB and a réunion to a couple of kilobytes; the
# ceiling is well clear of both and bounds what a single sitting can cost us.
MAX_RECORD_BYTES = 32 * 1024 * 1024
_default_getter = getter(timeout=60.0, max_bytes=MAX_RECORD_BYTES, error=DebatesFetchError)


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


def _agenda_subject(reunion: dict, odj_ref: str) -> str:
    """What the réunion's agenda says our item is about, e.g. « Suite de la
    discussion du projet de loi actualisant la programmation militaire… »."""
    points = as_list(((reunion.get("ODJ") or {}).get("pointsODJ") or {}).get("pointODJ"))
    for point in points:
        if isinstance(point, dict) and point.get("uid") == odj_ref:
            return str(point.get("objet") or "")
    return ""


#: Words that appear in every agenda line and every record title — the procedural
#: scaffolding of a sitting — and therefore tell two texts apart not at all.
_BOILERPLATE = frozenset(
    # articles and prepositions
    {
        "de",
        "du",
        "des",
        "la",
        "le",
        "les",
        "un",
        "une",
        "et",
        "en",
        "au",
        "aux",
        "pour",
        "sur",
        "dans",
        "par",
        "ou",
    }
    # the procedural scaffolding of an agenda line
    | {
        "projet",
        "proposition",
        "loi",
        "lois",
        "discussion",
        "suite",
        "lecture",
        "deuxieme",
        "premiere",
        "vote",
        "solennel",
        "conclusions",
        "commission",
        "mixte",
        "paritaire",
        "rapport",
        "texte",
        "visant",
        "relatif",
        "relative",
        "relatives",
        "diverses",
        "dispositions",
        "portant",
        "seance",
    }
)


def _significant(label: str) -> set[str]:
    """The words of a title that actually identify a subject.

    Accents folded and boilerplate removed, so « Suite de la discussion du projet
    de loi actualisant la programmation militaire » and « Actualisation de la
    programmation militaire » meet on {programmation, militaire, …} — which is
    the whole point, since the agenda and the record word the same subject
    differently.
    """
    folded = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode()
    return {
        w for w in re.findall(r"[a-z0-9]+", folded.lower()) if w not in _BOILERPLATE and len(w) > 2
    }


#: How much of the shorter of the two titles they must share, and how many words
#: at the very least, before we call it the same subject. Measured against the
#: corpus: real matches score 0.83 to 1.00 and everything else scores 0.
_SUBJECT_OVERLAP = 0.6
_SUBJECT_MIN_WORDS = 2


def _record_points(compte_rendu: dict) -> dict[str, str]:
    """``valeur_ptsodj`` → the subject the record's own summary gives it.

    The only place the two documents can be joined on meaning rather than on
    rank. The record carries no agenda identifier anywhere — checked, across the
    whole corpus — so this summary is what there is.
    """
    points: dict[str, str] = {}
    summary = ((compte_rendu.get("metadonnees") or {}).get("sommaire") or {}).get("sommaire1")
    for entry in as_list(summary):
        if not isinstance(entry, dict):
            continue
        tag = str((entry.get("@attributes") or {}).get("valeur_pts_odj") or "")
        title = (entry.get("titreStruct") or {}).get("intitule")
        if tag and isinstance(title, str) and title.strip():
            points.setdefault(tag, title.strip())
    return points


def _our_point(compte_rendu: dict, subject: str) -> str | None:
    """The record's own number for the agenda item ``subject``, or None."""
    wanted = _significant(subject)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for tag, title in _record_points(compte_rendu).items():
        shared = wanted & _significant(title)
        if len(shared) < _SUBJECT_MIN_WORDS:
            continue
        score = len(shared) / min(len(wanted), len(_significant(title)))
        if score > best_score:
            best, best_score = tag, score
    return best if best_score >= _SUBJECT_OVERLAP else None


#: How a compte rendu says several texts were taken together: « Discussion
#: générale commune », « Présentation commune ». Across 62 records it appears on
#: exactly the one joint debate and nowhere else, which is what makes it usable
#: as a signal rather than a guess.
_JOINT_SECTION = re.compile(r"commune?s?\b", re.IGNORECASE)


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


def speeches_from_record(compte_rendu: dict, *, subject: str, url: str) -> list[Speech]:
    """Extract the argued positions this record holds about ``subject``.

    ``subject`` is the réunion agenda's wording for our text. Everything hangs on
    locating it among the record's own points: read the wrong one and the
    presentation ends up quoting a debate about another bill, under this law's
    name, on the one page whose promise is that the quotes are real.
    """
    sections = _speech_sections(compte_rendu)
    wanted = _our_point(compte_rendu, subject)

    if wanted is None:
        # Our subject is nowhere in the record's summary. Either the sitting did
        # not discuss this text at all, or it discussed it *jointly* with
        # another, under a title naming neither or both. Only the second case is
        # recoverable, and only when the record says so itself.
        joint = [tag for tag, title in sections.items() if _JOINT_SECTION.search(title)]
        if len(joint) != 1:
            return []
        wanted = joint[0]
    elif wanted not in sections:
        # Found, and argued nowhere: our text was on the agenda that day for a
        # vote or a procedural step. Not a failure, and emphatically not a reason
        # to reach for somebody else's discussion.
        return []

    from_joint = bool(_JOINT_SECTION.search(sections[wanted]))

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
                joint_debate=from_joint,
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
            subject = _agenda_subject(reunion, odj_ref) if odj_ref else ""
            record = _get_json(get, cr_ref)
        except DebatesFetchError:
            continue
        legislature = (record.get("metadonnees") or {}).get("legislature") or "17"
        url = SEANCE_URL.format(legislature=legislature, uid=cr_ref)
        out.extend(speeches_from_record(record, subject=subject, url=url))
    return out
