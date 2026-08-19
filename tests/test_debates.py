"""Extracting attributed speeches from Assemblée nationale sitting records."""

import json
from datetime import date

from app.debates import DebatesFetchError, Speech, fetch_speeches, speeches_from_record

LONG = (
    "Cette proposition de loi répond à un besoin ancien et documenté. "
    "Elle organise le dépistage précoce et la prise en charge coordonnée, "
    "et elle fixe des objectifs mesurables pour les dix années à venir. "
) * 3


def _para(ptsodj, code, name, text, quality=None, role=None):
    orateur = {"nom": name, "id": "1"}
    orateur["qualite"] = quality if quality is not None else {}
    attrs = {"valeur_ptsodj": str(ptsodj), "code_grammaire": code}
    if role:
        attrs["roledebat"] = role
    return {"@attributes": attrs, "orateurs": {"orateur": orateur}, "texte": text}


def _record(points, uid="CRSANR5L17S2026O1N195", legislature="17"):
    return {
        "uid": uid,
        "seanceRef": "RUANR5L17S2026IDS30539",
        "metadonnees": {"dateSeance": "20260408090000000", "legislature": legislature},
        "contenu": {"point": points},
    }


def _titled(ptsodj, code, title, children):
    return {
        "@attributes": {"valeur_ptsodj": str(ptsodj), "code_grammaire": code},
        "texte": title,
        "point": children,
    }


ONE_BILL = _record([
    _titled(1, "DISC_GENERALE_1", "Discussion générale", [
        _para(1, "PAROLE_GENERIQUE", "Mme Louise Morel", LONG),
        _para(1, "PAROLE_GENERIQUE", "M. Jean Dupont", LONG, quality="rapporteur"),
        _para(1, "INTERRUPTION_1_10", "M. Bruyant", LONG),          # heckling
        _para(1, "PAROLE_GENERIQUE", "M. le président", LONG, role="president"),
        _para(1, "PAROLE_GENERIQUE", "Mme Brève", "Trop court."),   # below the floor
    ]),
])


def test_extracts_attributed_speeches():
    out = speeches_from_record(ONE_BILL, position=1, url="https://an.fr/cr")
    assert [s.speaker for s in out] == ["Mme Louise Morel", "M. Jean Dupont"]

    first = out[0]
    assert first.section == "Discussion générale"
    assert first.chamber == "Assemblée nationale"
    assert first.spoken_on == date(2026, 4, 8)
    assert first.url == "https://an.fr/cr"
    assert first.text.startswith("Cette proposition de loi")


def test_drops_interruptions_the_chair_and_one_liners():
    out = speeches_from_record(ONE_BILL, position=1, url="u")
    names = {s.speaker for s in out}
    assert "M. Bruyant" not in names       # INTERRUPTION grammar
    assert "M. le président" not in names  # chair, procedure not argument
    assert "Mme Brève" not in names        # below MIN_SPEECH_CHARS


def test_attribution_is_built_from_the_record():
    out = speeches_from_record(ONE_BILL, position=1, url="u")
    assert out[0].attribution == (
        "Mme Louise Morel, séance du 08/04/2026 · Assemblée nationale"
    )
    assert out[1].attribution == (
        "M. Jean Dupont (rapporteur), séance du 08/04/2026 · Assemblée nationale"
    )


TWO_BILLS = _record([
    _titled(1, "DISC_GENERALE_1", "Discussion générale", [
        _para(1, "PAROLE_GENERIQUE", "Mme Autre Loi", LONG),
    ]),
    _titled(2, "DISC_GENERALE_1", "Discussion générale", [
        _para(2, "PAROLE_GENERIQUE", "M. Notre Loi", LONG),
    ]),
])


def test_a_sitting_debating_two_bills_never_mixes_them():
    """The agenda position is what keeps another bill's arguments out."""
    ours = speeches_from_record(TWO_BILLS, position=2, url="u")
    assert [s.speaker for s in ours] == ["M. Notre Loi"]

    other = speeches_from_record(TWO_BILLS, position=1, url="u")
    assert [s.speaker for s in other] == ["Mme Autre Loi"]


def test_refuses_to_guess_when_the_bill_is_ambiguous():
    """Without a position, two argued bills in one sitting means no safe answer."""
    assert speeches_from_record(TWO_BILLS, position=None, url="u") == []
    # A single argued bill is unambiguous, so it still works.
    assert len(speeches_from_record(ONE_BILL, position=None, url="u")) == 2


def test_only_argumentative_sections_are_kept():
    articles = _record([
        _titled(1, "DISC_ARTICLES_2_4", "Article 3", [
            _para(1, "PAROLE_GENERIQUE", "M. Amendement", LONG),
        ]),
    ])
    assert speeches_from_record(articles, position=1, url="u") == []


def _getter(pages: dict[str, dict]):
    def get(url: str) -> bytes:
        uid = url.rsplit("/", 1)[-1].removesuffix(".json")
        if uid not in pages:
            raise DebatesFetchError(f"404 {uid}")
        return json.dumps(pages[uid]).encode()

    return get


REUNION = {
    "uid": "RUANR5L17S2026IDS30539",
    "compteRenduRef": "CRSANR5L17S2026O1N195",
    "ODJ": {"pointsODJ": {"pointODJ": [
        {"uid": "RUANR5L17S2026IDS30539PT1", "objet": "Autre texte"},
        {"uid": "RUANR5L17S2026IDS30539PT2", "objet": "Notre texte"},
    ]}},
}
SEANCES = [{"reunion_ref": "RUANR5L17S2026IDS30539",
            "odj_ref": "RUANR5L17S2026IDS30539PT2"}]


def test_resolves_sitting_then_record_and_picks_our_bill():
    get = _getter({"RUANR5L17S2026IDS30539": REUNION,
                   "CRSANR5L17S2026O1N195": TWO_BILLS})
    out = fetch_speeches(SEANCES, get=get)
    # PT2 is second on the agenda, so agenda position 2 — our bill, not the other.
    assert [s.speaker for s in out] == ["M. Notre Loi"]
    assert out[0].url.endswith("/comptes-rendus/seance/CRSANR5L17S2026O1N195")


def test_an_unreadable_sitting_is_skipped_not_fatal():
    """Partial coverage beats no arguments at all."""
    good = dict(REUNION, uid="RUANR5L17S2026IDS30540",
                compteRenduRef="CRSANR5L17S2026O1N195")
    get = _getter({"RUANR5L17S2026IDS30540": good,
                   "CRSANR5L17S2026O1N195": TWO_BILLS})
    seances = [
        {"reunion_ref": "MISSING", "odj_ref": "X"},
        {"reunion_ref": "RUANR5L17S2026IDS30540",
         "odj_ref": "RUANR5L17S2026IDS30539PT2"},
    ]
    assert len(fetch_speeches(seances, get=get)) == 1


def test_no_sittings_means_no_speeches():
    assert fetch_speeches([], get=_getter({})) == []


def test_speech_is_the_shape_the_selector_expects():
    s = Speech(speech_id="a-1", speaker="Mme X", quality="", chamber="Assemblée nationale",
               section="Discussion générale", spoken_on=date(2026, 4, 8), text="…", url="u")
    assert "Mme X" in s.attribution and "08/04/2026" in s.attribution
