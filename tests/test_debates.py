"""Extracting attributed speeches from Assemblée nationale sitting records."""

import json
from datetime import date

import pytest

from app.debates import (
    DEFAULT_LEGISLATURE,
    DebatesFetchError,
    Speech,
    fetch_speeches,
    speeches_from_record,
)

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


def _record(points, uid="CRSANR5L17S2026O1N195", legislature="17", sommaire=None):
    """A compte rendu. `sommaire` maps valeur_ptsodj -> the subject that point covers.

    It is not decoration: the record's summary is the only place the two
    documents can be joined on meaning, so a fixture without one is a record the
    extractor cannot place. See `app.debates._record_points`.
    """
    sommaire = sommaire or {"1": "Dépistage précoce des maladies cardio-neuro-vasculaires"}
    return {
        "uid": uid,
        "seanceRef": "RUANR5L17S2026IDS30539",
        "metadonnees": {
            "dateSeance": "20260408090000000",
            "legislature": legislature,
            "sommaire": {
                "sommaire1": [
                    {"@attributes": {"valeur_pts_odj": tag}, "titreStruct": {"intitule": title}}
                    for tag, title in sommaire.items()
                ]
            },
        },
        "contenu": {"point": points},
    }


#: What the réunion's agenda calls the text the ONE_BILL fixture is about. Worded
#: differently from the record's own title on purpose — that difference is the
#: thing the subject match exists to bridge.
OURS = (
    "Discussion de la proposition de loi visant à accélérer le dépistage "
    "précoce des maladies cardio-neuro-vasculaires"
)


def _titled(ptsodj, code, title, children):
    return {
        "@attributes": {"valeur_ptsodj": str(ptsodj), "code_grammaire": code},
        "texte": title,
        "point": children,
    }


ONE_BILL = _record(
    [
        _titled(
            1,
            "DISC_GENERALE_1",
            "Discussion générale",
            [
                _para(1, "PAROLE_GENERIQUE", "Mme Louise Morel", LONG),
                _para(1, "PAROLE_GENERIQUE", "M. Jean Dupont", LONG, quality="rapporteur"),
                _para(1, "INTERRUPTION_1_10", "M. Bruyant", LONG),  # heckling
                _para(1, "PAROLE_GENERIQUE", "M. le président", LONG, role="president"),
                _para(1, "PAROLE_GENERIQUE", "Mme Brève", "Trop court."),  # below the floor
            ],
        ),
    ]
)


def test_extracts_attributed_speeches():
    out = speeches_from_record(ONE_BILL, subject=OURS, url="https://an.fr/cr")
    assert [s.speaker for s in out] == ["Mme Louise Morel", "M. Jean Dupont"]

    first = out[0]
    assert first.section == "Discussion générale"
    assert first.chamber == "Assemblée nationale"
    assert first.spoken_on == date(2026, 4, 8)
    assert first.url == "https://an.fr/cr"
    assert first.text.startswith("Cette proposition de loi")


def test_drops_interruptions_the_chair_and_one_liners():
    out = speeches_from_record(ONE_BILL, subject=OURS, url="u")
    names = {s.speaker for s in out}
    assert "M. Bruyant" not in names  # INTERRUPTION grammar
    assert "M. le président" not in names  # chair, procedure not argument
    assert "Mme Brève" not in names  # below MIN_SPEECH_CHARS


def test_attribution_is_built_from_the_record():
    out = speeches_from_record(ONE_BILL, subject=OURS, url="u")
    assert out[0].attribution == ("Mme Louise Morel, séance du 08/04/2026 · Assemblée nationale")
    assert out[1].attribution == (
        "M. Jean Dupont (rapporteur), séance du 08/04/2026 · Assemblée nationale"
    )


TWO_BILLS = _record(
    [
        _titled(
            1,
            "DISC_GENERALE_1",
            "Discussion générale",
            [
                _para(1, "PAROLE_GENERIQUE", "Mme Autre Loi", LONG),
            ],
        ),
        _titled(
            2,
            "DISC_GENERALE_1",
            "Discussion générale",
            [
                _para(2, "PAROLE_GENERIQUE", "M. Notre Loi", LONG),
            ],
        ),
    ],
    sommaire={
        "1": "Restitution de biens culturels",
        "2": "Dépistage précoce des maladies cardio-neuro-vasculaires",
    },
)


def test_a_sitting_debating_two_bills_never_mixes_them():
    """The subject, not the rank, is what keeps another bill's arguments out."""
    ours = speeches_from_record(TWO_BILLS, subject=OURS, url="u")
    assert [s.speaker for s in ours] == ["M. Notre Loi"]

    other = speeches_from_record(
        TWO_BILLS,
        subject="Proposition de loi relative à la restitution de biens culturels",
        url="u",
    )
    assert [s.speaker for s in other] == ["Mme Autre Loi"]


def test_the_rank_in_the_agenda_is_never_used_as_the_record_s_number():
    """The bug this replaces, in one assertion.

    Our text was agenda item 2 and the record numbers it 2 here — but the record
    is free to number it anything, and did: on 5 May 2026 the loi de programmation
    militaire was agenda item 7, record point 5, and the record's only argued
    section was number 4 — a debate on social-security fraud. Reading by rank was
    one coincidence away from quoting the wrong bill under this law's name.
    """
    shifted = _record(
        [
            _titled(
                7,
                "DISC_GENERALE_1",
                "Discussion générale",
                [_para(7, "PAROLE_GENERIQUE", "M. Notre Loi", LONG)],
            ),
        ],
        sommaire={"7": "Dépistage précoce des maladies cardio-neuro-vasculaires"},
    )
    out = speeches_from_record(shifted, subject=OURS, url="u")
    assert [s.speaker for s in out] == ["M. Notre Loi"], "the subject must be found at any number"


def test_a_text_on_the_agenda_but_not_argued_takes_nobody_else_s_speeches():
    """Present for a vote, discussed by nobody: the honest answer is nothing.

    The tempting failure is to fall back on whatever single discussion the
    sitting did hold — which, on the real records, belonged to another bill.
    """
    others_only = _record(
        [
            _titled(
                1,
                "DISC_GENERALE_1",
                "Discussion générale",
                [_para(1, "PAROLE_GENERIQUE", "Mme Autre Loi", LONG)],
            ),
        ],
        sommaire={
            "1": "Restitution de biens culturels",
            "2": "Dépistage précoce des maladies cardio-neuro-vasculaires",
        },
    )
    assert speeches_from_record(others_only, subject=OURS, url="u") == []


def test_refuses_to_guess_when_the_subject_is_nowhere_in_the_record():
    """No match and no joint discussion means no safe answer."""
    unrelated = _record(
        [
            _titled(
                1,
                "DISC_GENERALE_1",
                "Discussion générale",
                [_para(1, "PAROLE_GENERIQUE", "Mme Autre Loi", LONG)],
            ),
        ],
        sommaire={"1": "Restitution de biens culturels"},
    )
    assert speeches_from_record(unrelated, subject=OURS, url="u") == []


# --- Discussions communes -----------------------------------------------------
#
# The Assemblée regularly takes several texts together: one presentation, one
# discussion générale, one record point covering them all. The record then names
# the joint subject and not ours, so nothing matches — which used to mean a law
# with twenty sittings and not one argument. The real case: « fin de vie »,
# debated with « soins palliatifs » on 12 May 2025, 46 attributed speeches
# discarded.


JOINT = _record(
    [
        _titled(
            1,
            "DISC_GENERALE_1",
            "Discussion générale commune",
            [_para(1, "PAROLE_GENERIQUE", "M. Deux Textes", LONG)],
        ),
    ],
    sommaire={"1": "Accompagnement et soins palliatifs – Droit à l’aide à mourir"},
)


def test_a_joint_discussion_is_kept_rather_than_lost():
    """Neither title names our text, so only « commune » can rescue these."""
    out = speeches_from_record(
        JOINT, subject="Proposition de loi relative à la fin de vie", url="u"
    )
    assert [s.speaker for s in out] == ["M. Deux Textes"]


def test_a_speech_from_a_joint_discussion_says_so():
    """The flag is the whole basis on which keeping them is defensible.

    A verified quote from a joint debate is genuinely what its author said, on a
    day this text was discussed — but they may have been arguing about the
    neighbouring one. Nothing downstream can tell, so the draft has to warn the
    person who can.
    """
    out = speeches_from_record(
        JOINT, subject="Proposition de loi relative à la fin de vie", url="u"
    )
    assert all(s.joint_debate for s in out)
    # And an ordinary discussion is not flagged, or the warning would mean nothing.
    assert not any(s.joint_debate for s in speeches_from_record(ONE_BILL, subject=OURS, url="u"))


def test_a_joint_discussion_is_not_a_licence_to_take_any_debate():
    """« commune » is a claim the record makes; without it, nothing is taken."""
    not_joint = _record(
        [
            _titled(
                1,
                "DISC_GENERALE_1",
                "Discussion générale",
                [_para(1, "PAROLE_GENERIQUE", "Mme Autre Loi", LONG)],
            ),
        ],
        sommaire={"1": "Restitution de biens culturels"},
    )
    assert speeches_from_record(not_joint, subject=OURS, url="u") == []


def test_two_joint_discussions_in_one_sitting_are_still_refused():
    """Two candidates and no way to choose is exactly when to take nothing."""
    ambiguous = _record(
        [
            _titled(
                1,
                "DISC_GENERALE_1",
                "Discussion générale commune",
                [_para(1, "PAROLE_GENERIQUE", "M. Un", LONG)],
            ),
            _titled(
                2,
                "DISC_GENERALE_1",
                "Discussion générale commune",
                [_para(2, "PAROLE_GENERIQUE", "M. Deux", LONG)],
            ),
        ],
        sommaire={"1": "Restitution de biens culturels", "2": "Fiscalité des successions"},
    )
    assert speeches_from_record(ambiguous, subject=OURS, url="u") == []


def test_only_argumentative_sections_are_kept():
    articles = _record(
        [
            _titled(
                1,
                "DISC_ARTICLES_2_4",
                "Article 3",
                [
                    _para(1, "PAROLE_GENERIQUE", "M. Amendement", LONG),
                ],
            ),
        ]
    )
    assert speeches_from_record(articles, subject=OURS, url="u") == []


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
    "ODJ": {
        "pointsODJ": {
            "pointODJ": [
                {
                    "uid": "RUANR5L17S2026IDS30539PT1",
                    "objet": "Discussion de la proposition de loi relative à la "
                    "restitution de biens culturels",
                },
                {"uid": "RUANR5L17S2026IDS30539PT2", "objet": OURS},
            ]
        }
    },
}
SEANCES = [{"reunion_ref": "RUANR5L17S2026IDS30539", "odj_ref": "RUANR5L17S2026IDS30539PT2"}]


def test_resolves_sitting_then_record_and_picks_our_bill():
    get = _getter({"RUANR5L17S2026IDS30539": REUNION, "CRSANR5L17S2026O1N195": TWO_BILLS})
    out = fetch_speeches(SEANCES, get=get)
    # PT2's subject is found in the record's own summary — our bill, not the other,
    # and regardless of where either sits in the agenda.
    assert [s.speaker for s in out] == ["M. Notre Loi"]
    assert out[0].url.endswith("/comptes-rendus/seance/CRSANR5L17S2026O1N195")


def test_an_unreadable_sitting_is_skipped_not_fatal():
    """Partial coverage beats no arguments at all."""
    good = dict(REUNION, uid="RUANR5L17S2026IDS30540", compteRenduRef="CRSANR5L17S2026O1N195")
    get = _getter({"RUANR5L17S2026IDS30540": good, "CRSANR5L17S2026O1N195": TWO_BILLS})
    seances = [
        {"reunion_ref": "MISSING", "odj_ref": "X"},
        {"reunion_ref": "RUANR5L17S2026IDS30540", "odj_ref": "RUANR5L17S2026IDS30539PT2"},
    ]
    assert len(fetch_speeches(seances, get=get)) == 1


def test_a_record_is_downloaded_once_however_many_agenda_items_point_at_it():
    """A compte rendu is ~600 kB, and a sitting can carry two of our agenda items.

    The dossier records those as two sittings — they are two agenda items, and
    the pair (réunion, point) is what identifies them — so the loop fetched the
    same réunion and the same record twice, over a link the drafting job is
    already racing a deadline on.
    """
    fetched: list[str] = []
    inner = _getter({"RUANR5L17S2026IDS30539": REUNION, "CRSANR5L17S2026O1N195": TWO_BILLS})

    def counting(url: str) -> bytes:
        fetched.append(url.rsplit("/", 1)[-1].removesuffix(".json"))
        return inner(url)

    two_items_one_sitting = [
        {"reunion_ref": "RUANR5L17S2026IDS30539", "odj_ref": "RUANR5L17S2026IDS30539PT1"},
        {"reunion_ref": "RUANR5L17S2026IDS30539", "odj_ref": "RUANR5L17S2026IDS30539PT2"},
    ]
    fetch_speeches(two_items_one_sitting, get=counting)

    assert fetched.count("CRSANR5L17S2026O1N195") == 1, "the record was downloaded twice"
    assert fetched.count("RUANR5L17S2026IDS30539") == 1, "the réunion was fetched twice"


def test_a_sitting_that_fails_is_retried_for_the_next_item():
    """Only successes are remembered — a failure must not be cached as one."""
    attempts: list[str] = []

    def flaky(url: str) -> bytes:
        uid = url.rsplit("/", 1)[-1].removesuffix(".json")
        attempts.append(uid)
        if uid == "RUANR5L17S2026IDS30539" and attempts.count(uid) == 1:
            raise DebatesFetchError("réseau")
        return _getter({"RUANR5L17S2026IDS30539": REUNION, "CRSANR5L17S2026O1N195": TWO_BILLS})(url)

    two_items_one_sitting = [
        {"reunion_ref": "RUANR5L17S2026IDS30539", "odj_ref": "RUANR5L17S2026IDS30539PT1"},
        {"reunion_ref": "RUANR5L17S2026IDS30539", "odj_ref": "RUANR5L17S2026IDS30539PT2"},
    ]
    out = fetch_speeches(two_items_one_sitting, get=flaky)

    assert attempts.count("RUANR5L17S2026IDS30539") == 2, "the failure was cached"
    assert [sp.speaker for sp in out] == ["M. Notre Loi"]


def test_a_spent_budget_stops_the_loop_rather_than_skipping_every_sitting():
    """A missing record is skipped; a spent clock is not — the two differ.

    ``DebatesFetchError`` means "this sitting could not be read", and carrying on
    is right. ``DeadlineExceeded`` means every remaining request will fail
    instantly, so carrying on walks twenty sittings to arrive nowhere. It is
    deliberately not the module's own error class, which is what makes the
    `except DebatesFetchError` below it not catch it.
    """
    from app.opendata import DeadlineExceeded

    def spent(url: str) -> bytes:
        raise DeadlineExceeded("délai épuisé")

    with pytest.raises(DeadlineExceeded):
        fetch_speeches(SEANCES * 20, get=spent)


def test_no_sittings_means_no_speeches():
    assert fetch_speeches([], get=_getter({})) == []


def test_speech_is_the_shape_the_selector_expects():
    s = Speech(
        speech_id="a-1",
        speaker="Mme X",
        quality="",
        chamber="Assemblée nationale",
        section="Discussion générale",
        spoken_on=date(2026, 4, 8),
        text="…",
        url="u",
    )
    assert "Mme X" in s.attribution and "08/04/2026" in s.attribution


def test_an_identifier_that_is_not_one_is_never_fetched():
    """The uid goes into a URL, so it must look like a uid before it gets there.

    It reaches us from the government export by way of our own staging table.
    This is not a suspicion about the Assemblée nationale: it is that a value
    which ends up in an outbound request should be one we recognise, whatever
    path it took. A `../` here would aim the fetch at another document.
    """
    asked: list[str] = []

    def get(url: str) -> bytes:
        asked.append(url)
        return b"{}"

    speeches = fetch_speeches([{"reunion_ref": "../../etc/passwd", "odj_ref": "x"}], get=get)

    assert speeches == []
    assert asked == [], "a malformed identifier must not reach the network"


def test_an_ordinary_identifier_still_goes_through():
    asked: list[str] = []

    def get(url: str) -> bytes:
        asked.append(url)
        return b"{}"

    fetch_speeches([{"reunion_ref": "RUANR5L17S2026PO861108N4", "odj_ref": "x"}], get=get)
    assert asked, "a real identifier must still be fetched"
    assert "RUANR5L17S2026PO861108N4" in asked[0]


def _with_legislature(value):
    """The two-bill record, restated as coming from a given legislature."""
    record = json.loads(json.dumps(TWO_BILLS))
    record.setdefault("metadonnees", {})["legislature"] = value
    return _getter({"RUANR5L17S2026IDS30539": REUNION, "CRSANR5L17S2026O1N195": record})


def test_a_legislature_that_is_not_one_never_reaches_the_source_url():
    """The sitting identifier is checked before it goes in a URL; so is this one.

    `legislature` is read out of a fetched document and lands in the `source`
    href of a published argument — the one field on the page whose whole promise
    is that a reader can follow it and check the quote.
    """
    out = fetch_speeches(SEANCES, get=_with_legislature("../../ailleurs"))
    assert out, "the speeches themselves must still be found"
    for speech in out:
        assert speech.url.startswith(
            f"https://www.assemblee-nationale.fr/dyn/{DEFAULT_LEGISLATURE}/"
        )
        assert ".." not in speech.url


def test_an_ordinary_legislature_is_still_used():
    """The check refuses what is not a legislature; it does not pin the value."""
    out = fetch_speeches(SEANCES, get=_with_legislature("18"))
    assert out
    assert all("/dyn/18/" in s.url for s in out)
