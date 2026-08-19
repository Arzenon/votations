"""Selecting arguments from sitting records — and refusing anything unverifiable."""

from datetime import date

from app import ai
from app.ai import AiDraftError, ArgumentPick, ArgumentSelection, select_arguments
from app.config import get_settings
from app.debates import Speech
from app.legifrance import Article, LawText

LAW = LawText(
    nor="SFHX2609544L", law_code="2026-668",
    title="LOI n° 2026-668 du 27 juillet 2026 portant diverses dispositions",
    nature="LOI", signed_on=date(2026, 7, 27), published_on=date(2026, 7, 28),
    eli_url="https://www.legifrance.gouv.fr/eli/loi/2026/7/27/2026-668/jo/texte",
    articles=[Article(num="1", text="Le code de la santé publique est ainsi modifié.")],
)

FOR_TEXT = (
    "Ce texte répond à un besoin ancien et documenté par les autorités sanitaires. "
    "Il organise le dépistage précoce et fixe des objectifs mesurables."
)
AGAINST_TEXT = (
    "Ce texte ne prévoit aucun financement nouveau pour les hôpitaux concernés. "
    "Nous ne pouvons pas voter des objectifs sans les moyens correspondants."
)

QUOTE_FOR = "Il organise le dépistage précoce et fixe des objectifs mesurables."
QUOTE_AGAINST = "Nous ne pouvons pas voter des objectifs sans les moyens correspondants."

SPEECHES = [
    Speech(speech_id="cr-1", speaker="Mme Louise Morel", quality="rapporteure",
           chamber="Assemblée nationale", section="Discussion générale",
           spoken_on=date(2026, 4, 8), text=FOR_TEXT, url="https://an.fr/cr#1"),
    Speech(speech_id="cr-2", speaker="M. Paul Vasseur", quality="",
           chamber="Assemblée nationale", section="Discussion générale",
           spoken_on=date(2026, 4, 8), text=AGAINST_TEXT, url="https://an.fr/cr#2"),
]


class _FakeClient:
    def __init__(self, selection, fail=None):
        self.selection, self.fail, self.seen = selection, fail, {}
        outer = self

        class _Messages:
            def count_tokens(self, **kw):
                return type("R", (), {"input_tokens": 1000})()

            def parse(self, **kw):
                outer.seen = kw
                if outer.fail:
                    raise outer.fail
                return type("R", (), {"parsed_output": outer.selection})()

        self.messages = _Messages()


def _sel(pour=(), contre=(), reserves=()):
    return ArgumentSelection(
        pour=[ArgumentPick(speech_id=i, quote=q) for i, q in pour],
        contre=[ArgumentPick(speech_id=i, quote=q) for i, q in contre],
        reserves=[ArgumentPick(speech_id=i, quote=q) for i, q in reserves],
    )


def test_keeps_a_verbatim_quote_and_attributes_it_from_our_record():
    quote_for = QUOTE_FOR
    quote_against = "Ce texte ne prévoit aucun financement nouveau pour les hôpitaux concernés."
    client = _FakeClient(_sel([("cr-1", quote_for)], [("cr-2", quote_against)]))

    out = select_arguments(LAW, SPEECHES, client=client)

    assert out.pour[0]["text"] == quote_for
    assert out.contre[0]["text"] == quote_against
    # Attribution and link come from the Speech, never from the model.
    assert out.pour[0]["author"] == (
        "Mme Louise Morel (rapporteure), séance du 08/04/2026 · Assemblée nationale"
    )
    assert out.pour[0]["source"] == "https://an.fr/cr#1"
    assert out.is_symmetric


def test_a_paraphrase_is_rejected():
    """The whole design: a quote that is not in the speech cannot survive."""
    invented = "Ce texte est une avancée majeure pour la santé publique française."
    assert invented not in FOR_TEXT
    client = _FakeClient(_sel(
        [("cr-1", invented),
         ("cr-1", QUOTE_FOR)],
        [("cr-2", QUOTE_AGAINST)],
    ))

    out = select_arguments(LAW, SPEECHES, client=client)

    assert len(out.pour) == 1
    assert out.pour[0]["text"] != invented
    assert any("introuvable" in n for n in out.notes)


def test_a_real_quote_cannot_be_pinned_on_the_wrong_speaker():
    """The model supplies no names, so misattribution is not expressible."""
    # Quote genuinely from cr-2, but claimed as cr-1.
    client = _FakeClient(_sel(
        [("cr-1", "Ce texte ne prévoit aucun financement nouveau pour les hôpitaux")],
        [("cr-2", QUOTE_AGAINST)],
    ))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.pour == []
    assert any("introuvable" in n for n in out.notes)


def test_unknown_speech_id_is_rejected():
    client = _FakeClient(_sel(
        [("does-not-exist", "Il organise le dépistage précoce")],
        [("cr-2", QUOTE_AGAINST)],
    ))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.pour == []
    assert any("identifiant inconnu" in n for n in out.notes)


def test_a_snippet_too_short_to_be_an_argument_is_rejected():
    client = _FakeClient(_sel([("cr-1", "Ce texte")], [("cr-2", QUOTE_AGAINST)]))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.pour == []
    assert any("trop court" in n for n in out.notes)


def test_whitespace_differences_do_not_break_a_genuine_quote():
    messy = "  Il organise le dépistage\n   précoce et fixe des\tobjectifs mesurables.  "
    client = _FakeClient(_sel([("cr-1", messy)]))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.pour[0]["text"] == QUOTE_FOR


def test_a_unanimous_law_yields_an_empty_side_with_a_note():
    """Manufactured balance is the worse failure — say so instead."""
    client = _FakeClient(_sel([("cr-1", QUOTE_FOR)]))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.pour and out.contre == []
    assert not out.is_symmetric
    assert any("déséquilibre" in n for n in out.notes)


def test_a_supporters_misgiving_is_a_reserve_not_an_argument_against():
    """The wrinkle on a consensual law: criticism exists, opposition does not."""
    client = _FakeClient(_sel(
        pour=[("cr-1", QUOTE_FOR)],
        reserves=[("cr-2", QUOTE_AGAINST)],
    ))
    out = select_arguments(LAW, SPEECHES, client=client)

    assert out.contre == [], "a reservation must never be filed as opposition"
    assert len(out.reserves) == 1
    assert out.reserves[0]["text"] == QUOTE_AGAINST
    # Attributed exactly like an argument, from our record.
    assert out.reserves[0]["author"].startswith("M. Paul Vasseur")
    assert out.reserves[0]["source"] == "https://an.fr/cr#2"

    note = " ".join(out.notes)
    assert "Aucune opposition au texte" in note
    assert "jamais comme des arguments d'opposants" in note


def test_reserves_are_verified_verbatim_like_everything_else():
    client = _FakeClient(_sel(
        pour=[("cr-1", QUOTE_FOR)],
        reserves=[("cr-2", "une réserve que personne n'a jamais formulée en séance")],
    ))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.reserves == []
    assert any("introuvable" in n for n in out.notes)


def test_reserves_alone_are_enough_to_be_worth_showing():
    client = _FakeClient(_sel(reserves=[("cr-2", QUOTE_AGAINST)]))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.reserves and not out.pour and not out.contre


def test_real_opposition_still_takes_precedence_in_the_note():
    client = _FakeClient(_sel(
        pour=[("cr-1", QUOTE_FOR)],
        contre=[("cr-2", QUOTE_AGAINST)],
        reserves=[("cr-2", "Ce texte ne prévoit aucun financement nouveau pour les hôpitaux")],
    ))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert out.is_symmetric
    assert len(out.reserves) == 1
    assert not any("Aucune opposition" in n for n in out.notes)


def test_everything_rejected_is_an_error_not_an_empty_page():
    client = _FakeClient(_sel([("cr-1", "phrase totalement inventée par le modèle")]))
    try:
        select_arguments(LAW, SPEECHES, client=client)
        raise AssertionError("expected AiDraftError")
    except AiDraftError as exc:
        assert "vérifié" in str(exc)


def test_no_speeches_is_an_error():
    try:
        select_arguments(LAW, [], client=_FakeClient(_sel()))
        raise AssertionError("expected AiDraftError")
    except AiDraftError:
        pass


def test_per_side_cap_is_enforced():
    picks = [("cr-1", QUOTE_FOR)] * 5
    client = _FakeClient(_sel(picks))
    out = select_arguments(LAW, SPEECHES, client=client, per_side=2)
    assert len(out.pour) == 2


def test_caps_default_to_the_configured_values():
    """Arguments are a pool to curate from; réserves stay tighter."""
    settings = get_settings()
    client = _FakeClient(_sel(
        pour=[("cr-1", QUOTE_FOR)] * 9,
        contre=[("cr-2", QUOTE_AGAINST)] * 9,
        reserves=[("cr-2", QUOTE_AGAINST)] * 9,
    ))
    out = select_arguments(LAW, SPEECHES, client=client)
    assert len(out.pour) == settings.ai_arguments_per_side
    assert len(out.contre) == settings.ai_arguments_per_side
    assert len(out.reserves) == settings.ai_reserves_max
    assert settings.ai_reserves_max <= settings.ai_arguments_per_side


def test_the_model_is_told_the_numbers_are_ceilings_not_quotas():
    """Padding to hit a quota is the failure this guards against."""
    client = _FakeClient(_sel([("cr-1", QUOTE_FOR)]))
    select_arguments(LAW, SPEECHES, client=client, per_side=5, max_reserves=3)
    prompt = client.seen["messages"][0]["content"]
    assert "au plus 5 passages pour chaque camp" in prompt
    assert "au plus 3 réserves" in prompt
    assert "n'atteins ces nombres que si" in prompt


def test_oversized_pools_are_trimmed_and_reported():
    big = [
        Speech(speech_id=f"cr-{i}", speaker=f"M. {i}", quality="",
               chamber="Assemblée nationale", section="Discussion générale",
               spoken_on=date(2026, 4, 8), text="mot " * 20000, url="u")
        for i in range(40)
    ]
    client = _FakeClient(_sel())
    try:
        select_arguments(LAW, big, client=client)
    except AiDraftError:
        pass
    prompt = client.seen["messages"][0]["content"]
    assert len(prompt) < 700_000  # bounded, not the whole pool


def test_prompt_tells_the_model_to_copy_not_to_write():
    system = ai.ARGUMENTS_SYSTEM_PROMPT
    assert "copiée à l'identique" in system
    assert "Ne reformule pas" in system
    assert "liste « contre » vide" in system  # empty side is allowed
    assert "`reserves`" in system                # the third category is explained
    assert "N'invente jamais d'identifiant" in system


def test_api_failure_becomes_a_readable_error():
    client = _FakeClient(_sel(), fail=RuntimeError("overloaded_error"))
    try:
        select_arguments(LAW, SPEECHES, client=client)
        raise AssertionError("expected AiDraftError")
    except AiDraftError as exc:
        assert "overloaded_error" in str(exc)
