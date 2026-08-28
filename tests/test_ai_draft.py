"""AI-assisted drafting of the two factual presentation blocks."""

import base64
from datetime import date

import pytest
from markupsafe import escape

from app import ai
from app.ai import AiDraftError, LawDraft, generate_draft
from app.config import get_settings
from app.crud import imports as imports_crud
from app.legifrance import Article, LawText
from app.models import (
    DRAFT_FAILED,
    DRAFT_IDLE,
    DRAFT_PENDING,
    DRAFT_READY,
    STATUS_IMPORTED,
    ImportedLaw,
    Law,
)
from tests.conftest import drain_drafts
from tests.helpers import make_law


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin_auth() -> dict:
    s = get_settings()
    return _basic(s.admin_username, s.admin_password)


LAW = LawText(
    nor="SFHX2609544L",
    law_code="2026-668",
    title="LOI n° 2026-668 du 27 juillet 2026 portant diverses dispositions",
    nature="LOI",
    signed_on=date(2026, 7, 27),
    eli_url="https://www.legifrance.gouv.fr/eli/loi/2026/7/27/2026-668/jo/texte",
    articles=[Article(num="1", text="Le code de la santé publique est ainsi modifié.")],
)

DRAFT = LawDraft(
    summary_text="La loi modifie le code de la santé publique.",
    practical_changes="Les patients concernés bénéficient d'un nouveau droit.",
)


class _FakeClient:
    """Stands in for anthropic.Anthropic, recording what it was asked."""

    def __init__(self, tokens=5000, draft=DRAFT, fail=None):
        self.tokens, self.draft, self.fail = tokens, draft, fail
        self.seen = {}
        outer = self

        class _Messages:
            def count_tokens(self, **kwargs):
                outer.seen["count"] = kwargs
                return type("R", (), {"input_tokens": outer.tokens})()

            def parse(self, **kwargs):
                outer.seen["parse"] = kwargs
                if outer.fail:
                    raise outer.fail
                return type("R", (), {"parsed_output": outer.draft})()

        self.messages = _Messages()


def test_generates_the_two_factual_blocks():
    client = _FakeClient()
    draft = generate_draft(LAW, client=client)
    assert draft.summary_text.startswith("La loi modifie")
    assert draft.practical_changes.startswith("Les patients")

    # The official text and its identity are what the model is given.
    sent = client.seen["parse"]
    assert sent["model"] == get_settings().ai_model
    assert sent["output_format"] is LawDraft
    prompt = sent["messages"][0]["content"]
    assert "Le code de la santé publique est ainsi modifié." in prompt
    assert LAW.title in prompt


def test_prompt_forbids_invention_and_position_taking():
    """The charter's neutrality rules must reach the model, not just the docs."""
    system = ai.SYSTEM_PROMPT
    assert "N'invente aucun fait" in system
    assert "Aucune prise de position" in system
    # Arguments are out of scope: the JO carries no attributable positions.
    assert "arguments" not in system.lower()


def test_the_second_block_is_asked_for_something_the_first_cannot_say():
    """« Ce que ça change » must not be « Ce que dit la loi » in plainer French.

    The first version of this prompt asked only for a change of *register* —
    « en langage accessible, sans jargon juridique » — and got what it asked
    for: on a real draft, every one of the six facts in the second block was
    already in the first. The reader paid twice for one payload, on the page
    that also carries the arguments and the ballot.

    So the prompt now asks the second block a different question, and says what
    to do when the honest answer is nothing.
    """
    system = ai.SYSTEM_PROMPT
    assert "pas le premier bloc" in system, "the restatement must be forbidden outright"
    assert "N'y répète pas ce qui est déjà dans summary_text" in system
    assert "laisse ce bloc vide" in system, "an empty block must be an allowed answer"
    # But allowed for one reason only. Left as "nothing remains to say once the
    # repetitions are removed", it fired on a law that plainly changed things for
    # farmers: the first block had been made concrete enough to cover the ground,
    # the no-repeat rule pointed the same way, and « ce que ça change en pratique »
    # came back as an empty string on a 61-article agriculture act.
    assert "Ne laisse ce bloc vide que si la loi ne crée d'obligation" in system
    assert "il est à écrire" in system, "an empty block must not be reachable by attrition"
    # And the tie-break that makes the no-repeat rule survivable: the reader's
    # side wins, so overlap shortens block 1 rather than emptying block 2.
    assert "elle appartient au \\\n second" in system or "elle appartient au" in system
    # Framed by who is affected, rather than by the shape of the text.
    assert "personnes concernées" in system
    assert "ne change pas" in system, "what stays the same is information too"


def test_the_two_blocks_are_told_to_divide_the_work_both_ways():
    """The no-repeat rule has to run in both directions, or it only moves the problem.

    Telling the second block not to restate the first left the first free to walk
    through the obligations the second was now covering — from the reader's side,
    and better. On a real draft, two of the first block's four paragraphs were
    what the second block already said.

    So the first block is asked for the law seen from outside — subject,
    mechanism, texts modified, scope, what is not in force — and told to leave the
    resulting duties alone.
    """
    system = ai.SYSTEM_PROMPT
    assert "l'idée de la loi, pas son plan" in system
    assert "N'y détaille pas les obligations" in system, "block 1 must defer the duties"
    assert "Les deux blocs ne se recouvrent pas" in system
    # But a law that binds nobody still has to be presentable in one block.
    assert "sera vide" in system


def test_the_second_block_is_held_short_per_audience_not_capped_in_audiences():
    """The brake belongs on each paragraph, not on how many people are named.

    Given a division of labour and no length rule, the second block grew to 21
    paragraphs and 3.7× the first — a law amending fifteen codes really does
    concern that many people. Capping the *number* would mean choosing whose
    situation goes unmentioned, which is the one editorial act this platform
    exists not to make. So the length is held per audience.

    And the constraint has to say what it is for. A model told only "be shorter"
    hedges everything into vagueness; this one is told to drop what matters least
    and keep the rest exact, because the reader who is affected needs the
    threshold and the date, not a gentler sentence.
    """
    system = ai.SYSTEM_PROMPT
    assert "Un paragraphe court par public concerné" in system
    assert "n'est pas plafonné" in system, "the number of audiences must stay free"
    # The count follows readers, not provisions — see the granularity test below,
    # which is how a sector-specific law stops producing nineteen paragraphs.
    assert "nombre de **lecteurs** concernés" in system

    # The cap must not be argued with in the same breath. It used to be: nine words
    # setting the limit, then ninety walking it back — « chacun portant beaucoup »,
    # « jamais pour te faire rester vague », « garde le seuil, la date ou le
    # montant », « Trois faits exacts valent mieux qu'une phrase floue sur dix ».
    # The model weighed the two sides and wrote longer: paragraphs went from 540 to
    # 747 characters on average, and those over 800 from one to seven.
    #
    # The tell is in this same prompt. Block 1's brevity rule carries no
    # counterweight and lands exactly — three paragraphs, on both laws, on every
    # run. So the fix was subtraction, plus somewhere for the overflow to go.
    for undercut in (
        "chacun portant beaucoup",
        "jamais pour te faire rester vague",
        "Trois faits exacts valent mieux",
    ):
        assert undercut not in system, f"the cap is undercut again by: {undercut}"
    # One clause of anti-vagueness survives — it was the load-bearing half. There
    # was briefly a second clause here sending the overflow to the official text;
    # it drew no uptake at all — zero mentions across a 16 000-character block — so
    # it went the way of any instruction nothing acts on.
    assert "va au texte officiel" not in system
    assert "sans jamais remplacer un fait précis par une formule générale" in system


def test_the_prompt_says_each_thing_once():
    """Duplicated rules drift, and the loosest wording wins.

    Measured on the real prompt: brevity was stated seven times in seven wordings,
    and an empty second block was licensed four times under two incompatible
    conditions — the permissive one (« si le texte ne permet pas de rédiger
    honnêtement un bloc ») sitting in final position, thirty lines after the strict
    one that was supposed to have replaced it. « Ce que ça change en pratique » then
    came back empty on a 61-article agriculture act.
    """
    system = ai.SYSTEM_PROMPT
    # The loose licence is gone; the precise one remains.
    assert "laisse-le vide plutôt que de le combler" not in system
    assert "Ne laisse ce bloc vide que si la loi ne crée d'obligation" in system
    # The ratification case is stated twice, and twice is right: once to say block 1
    # may then run longer, once to say block 2 may then be empty. Same condition,
    # two different consequences — that is working redundancy, not drift. What was
    # removed is the third statement, which carried no consequence of its own.
    assert system.count("ratification d'un accord") == 2
    assert "Si la loi se borne à ratifier un accord international" not in system
    # And block 1 no longer asks for the very thing its next rule forbids: it used
    # to want « par quels moyens elle agit » and then « pas la règle qu'elle y
    # produit », and the draft duly opened on a list of moyens.
    assert "par quels moyens elle agit" not in system
    assert "Nomme le domaine sur lequel la loi intervient" in system


def test_an_audience_is_how_someone_describes_themselves():
    """Granularity, not a cap — and the difference is the whole point.

    Measured on two real drafts: the loi de programmation militaire produced 22
    audiences and read well, because they are things people are — « un jeune
    Français », « si vous déménagez », « pour les communes ». The loi
    d'urgence agricole produced 19 and read as a specialist circular, because
    they are things articles address — « betteravier », « irrigant », « éleveur
    d'ovins », « si vous siégez en commission locale de l'eau ». Same length,
    same paragraph count, opposite usefulness: a reader outside farming appeared
    in none of the nineteen, and a farmer in two or three.

    So the fix cannot be a cap on the count. It is a rule about what counts as an
    audience — the word someone would use for themselves — with the specifics
    kept inside the paragraph.
    """
    system = ai.SYSTEM_PROMPT
    assert "façon dont quelqu'un se décrit lui-même" in system
    assert "ça, c'est moi" in system, "the test the model applies must be the reader's"
    # Regrouping must never become dropping: that is the editorial act the
    # platform exists not to make, and the reason the count stays uncapped.
    assert "Ne supprime jamais la mesure" in system
    assert "l'écarter parce que peu de gens sont concernés, jamais" in system
    # Most-concerned first, so stopping early still leaves you informed.
    assert "Commence par les publics les plus nombreux" in system


def test_the_first_block_is_written_for_someone_who_never_opened_a_code():
    """Three habits measured on the real drafts, each costing the reader.

    In the three drafts reviewed, the *last* paragraph of every « Ce que dit la
    loi » was administrative plumbing — decrees still to come, ordonnances
    extending the law overseas, transfers between administrations — and it ran to
    roughly a third of the block (1031 of 3046 characters on one, 932 of 2626 on
    another). Add a ten-code enumeration in a single 1272-character sentence, and
    article numbers a voter cannot use, and the block that decides whether
    someone keeps reading is the one written for a lawyer.
    """
    system = ai.SYSTEM_PROMPT
    assert "Ne cite aucun numéro d'article ni de code" in system
    assert "N'énumère pas les codes modifiés" in system
    assert "Ne consacre pas de paragraphe à la mécanique administrative" in system
    # But the censure itself is news a voter needs — only its numbering is not.
    assert "Dis *ce que* le Conseil" in system
    # And a territory still matters where the law does not reach.
    assert "ne s'y applique pas" in system


def test_measures_before_sending_and_refuses_outsized_text():
    settings = get_settings()
    client = _FakeClient(tokens=settings.ai_max_input_tokens + 1)
    try:
        generate_draft(LAW, client=client)
        raise AssertionError("expected AiDraftError")
    except AiDraftError as exc:
        assert "trop volumineux" in str(exc)
    # Refused before any billable generation call.
    assert "count" in client.seen
    assert "parse" not in client.seen


def test_refuses_a_text_with_no_articles():
    empty = LawText(
        nor="X",
        law_code="1",
        title="t",
        nature="LOI",
        signed_on=None,
        eli_url=None,
        articles=[],
    )
    client = _FakeClient()
    try:
        generate_draft(empty, client=client)
        raise AssertionError("expected AiDraftError")
    except AiDraftError:
        pass
    assert client.seen == {}


def test_api_failure_becomes_a_readable_error():
    client = _FakeClient(fail=RuntimeError("overloaded_error"))
    try:
        generate_draft(LAW, client=client)
        raise AssertionError("expected AiDraftError")
    except AiDraftError as exc:
        assert "overloaded_error" in str(exc)


def test_without_a_key_the_feature_reports_itself_unavailable():
    settings = get_settings()
    previous = settings.anthropic_api_key
    settings.anthropic_api_key = ""
    try:
        generate_draft(LAW)
        raise AssertionError("expected AiDraftError")
    except AiDraftError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    finally:
        settings.anthropic_api_key = previous


def _stage(db_session, uid: str = "DLR5L17N001") -> ImportedLaw:
    item = ImportedLaw(
        source="assemblee",
        dossier_uid=uid,
        legislature="17",
        procedure="Proposition de loi ordinaire",
        title="Loi de test",
        law_code="2026-668",
        promulgated_on=date(2026, 7, 27),
        jo_date=date(2026, 7, 28),
        nor="SFHX2609544L",
        legifrance_url="http://legifrance.gouv.fr/legacy",
        dossier_url="https://www.assemblee-nationale.fr/dyn/17/dossiers/DLR5L17N001",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _with_key(value: str):
    settings = get_settings()
    previous = settings.anthropic_api_key
    settings.anthropic_api_key = value
    return settings, previous


def test_draft_button_hidden_without_a_key(client, db_session):
    item = _stage(db_session)
    settings, previous = _with_key("")
    try:
        r = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
        assert "brouillon IA" not in r.text
    finally:
        settings.anthropic_api_key = previous


def test_draft_button_is_on_the_law_form_not_the_import_list(client, db_session):
    """The admin drafts from the form they are about to edit."""
    item = _stage(db_session)
    settings, previous = _with_key("sk-test")
    try:
        form = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
        assert "Proposer un brouillon IA" in form.text
        assert f'formaction="/admin/imports/{item.id}/brouillon"' in form.text
        listing = client.get("/admin/imports", headers=_admin_auth())
        assert "brouillon IA" not in listing.text
    finally:
        settings.anthropic_api_key = previous


def test_law_form_carries_the_source_links(client, db_session):
    """Both links are on the form, so no navigating back to the list."""
    item = _stage(db_session)
    r = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
    assert item.dossier_url in r.text
    assert "Dossier Assemblée nationale" in r.text
    assert "Texte sur Légifrance" in r.text
    assert "LOI n° 2026-668" in r.text


def _patch_job(monkeypatch, *, draft=DRAFT, calls=None, fail=None, arguments=None, speeches=("s",)):
    """Point the background drafting job at fakes instead of the network."""

    def fetch(nor, jo_date):
        if fail:
            raise fail
        return LAW

    def generate(law, *, client=None):
        if calls is not None:
            calls.append(1)
        return draft

    monkeypatch.setattr("app.drafting.fetch_law_text", fetch)
    monkeypatch.setattr("app.drafting.generate_draft", generate)
    monkeypatch.setattr("app.drafting.fetch_speeches", lambda seances: list(speeches))
    if arguments is not None:
        monkeypatch.setattr("app.drafting.select_arguments", lambda law, sp, client=None: arguments)


def _draft(client, item_id, **data):
    """Ask for a draft, then run the job the request queued.

    The route only schedules: it answers straight away and the form polls for the
    outcome. So the response below is what the administrator sees *while the job
    is still pending*, and the drain afterwards is the pool getting to it.
    """
    response = client.post(
        f"/admin/imports/{item_id}/brouillon", headers=_admin_auth(), data=data or None
    )
    drain_drafts()
    return response


def _form(client, item_id):
    return client.get(f"/admin/lois/nouvelle?import_id={item_id}", headers=_admin_auth())


def test_drafting_is_scheduled_not_awaited(client, db_session, background_db, monkeypatch):
    """Downloading JO archives and calling a model twice cannot happen in a request."""
    item = _stage(db_session)
    _patch_job(monkeypatch)

    r = _draft(client, item.id)
    assert r.status_code == 200
    # The answer comes back immediately, announcing work in progress...
    assert "Brouillon en cours de génération" in r.text
    # ...and says where to watch for the outcome.
    assert f'hx-get="/admin/imports/{item.id}/brouillon/etat"' in r.text


def test_the_background_job_stores_the_draft(client, db_session, background_db, monkeypatch):
    item = _stage(db_session)
    _patch_job(monkeypatch)

    _draft(client, item.id)

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft["summary_text"] == DRAFT.summary_text
    assert stored.drafted_at is not None
    assert stored.draft_status == "pret"
    # Still not a published law: a person decides that.
    assert stored.status == "nouveau"
    assert stored.law_id is None


def test_the_state_endpoint_reports_the_outcome(client, db_session, background_db, monkeypatch):
    item = _stage(db_session)
    _patch_job(monkeypatch)
    _draft(client, item.id)

    r = client.get(f"/admin/imports/{item.id}/brouillon/etat", headers=_admin_auth())
    assert r.status_code == 200
    assert "Le brouillon est prêt" in r.text
    # Terminal state ⇒ the fragment no longer asks to be polled.
    assert "hx-trigger" not in r.text


def test_the_draft_lands_in_the_editable_form(client, db_session, background_db, monkeypatch):
    item = _stage(db_session)
    _patch_job(monkeypatch)
    _draft(client, item.id)

    r = _form(client, item.id)
    # HTML-escaped (never marked |safe).
    assert str(escape(DRAFT.summary_text)) in r.text
    assert str(escape(DRAFT.practical_changes)) in r.text
    assert "d&#39;un" in r.text
    assert "Brouillon généré par IA" in r.text
    assert f'name="import_id" value="{item.id}"' in r.text
    # The durable ELI link is preferred over the legacy one.
    assert LAW.eli_url in r.text
    assert "legifrance.gouv.fr/legacy" not in r.text


def test_opening_the_form_shows_a_draft_already_paid_for(
    client, db_session, background_db, monkeypatch
):
    """No extra click to see work that has already been billed."""
    item = _stage(db_session)
    _patch_job(monkeypatch)
    _draft(client, item.id)

    r = _form(client, item.id)
    assert str(escape(DRAFT.summary_text)) in r.text
    assert "Revoir le brouillon IA" in r.text
    assert "Régénérer" in r.text


def test_a_cached_draft_is_reused_without_calling_the_model(
    client, db_session, background_db, monkeypatch
):
    """Generating is billable; reopening must be free."""
    item = _stage(db_session)
    calls = []
    _patch_job(monkeypatch, calls=calls)

    _draft(client, item.id)
    assert len(calls) == 1

    for _ in range(3):
        r = _draft(client, item.id)
        assert str(escape(DRAFT.summary_text)) in r.text
    assert len(calls) == 1, "reopening a draft must not call the model again"


def test_a_cached_draft_survives_a_restart(client, db_session, background_db, monkeypatch):
    """The cache is a database column, not process state."""
    item = _stage(db_session)
    _patch_job(monkeypatch)
    _draft(client, item.id)

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft["practical_changes"] == DRAFT.practical_changes

    # No model available at all now — the draft must still come back.
    def gone(law, *, client=None):
        raise AssertionError("must not call the model for a cached draft")

    monkeypatch.setattr("app.drafting.generate_draft", gone)
    r = _draft(client, item.id)
    assert str(escape(DRAFT.summary_text)) in r.text


def test_regenerating_is_explicit(client, db_session, background_db, monkeypatch):
    item = _stage(db_session)
    calls = []
    _patch_job(monkeypatch, calls=calls)

    _draft(client, item.id)
    _draft(client, item.id, regenerer="1")
    assert len(calls) == 2


def test_a_draft_never_overwrites_what_the_admin_typed(
    client, db_session, background_db, monkeypatch
):
    item = _stage(db_session)
    _patch_job(monkeypatch)

    mine = "Mon propre résumé, écrit à la main."
    r = _draft(client, item.id, summary_text=mine, title="Mon titre")
    assert str(escape(mine)) in r.text
    assert "Mon titre" in r.text

    # And still after the job lands: the cached draft fills only empty blocks.
    again = _draft(client, item.id, summary_text=mine, title="Mon titre")
    assert str(escape(mine)) in again.text
    assert str(escape(DRAFT.practical_changes)) in again.text


def test_the_published_checkbox_survives_a_draft(client, db_session, background_db, monkeypatch):
    """Asking for a draft used to silently unpublish the law being prepared."""
    item = _stage(db_session)
    _patch_job(monkeypatch)
    r = _draft(client, item.id, published="1", title="Mon titre")
    checkbox = r.text.split('name="published"')[1].split(">")[0]
    assert "checked" in checkbox


def test_draft_failure_is_reported_without_losing_the_admins_work(
    client, db_session, background_db, monkeypatch
):
    item = _stage(db_session)
    _patch_job(monkeypatch, fail=ai.AiDraftError("texte introuvable pour le NOR SFHX2609544L"))

    mine = "Ce que j'avais déjà écrit."
    r = _draft(client, item.id, summary_text=mine)
    assert r.status_code == 200
    # The admin stays on the form with their work intact.
    assert str(escape(mine)) in r.text

    state = client.get(f"/admin/imports/{item.id}/brouillon/etat", headers=_admin_auth())
    assert "Brouillon impossible" in state.text
    assert "texte introuvable" in state.text
    assert imports_crud.count_new(db_session) == 1


def test_a_draft_without_a_nor_fails_cleanly(client, db_session, background_db, monkeypatch):
    item = _stage(db_session)
    item.nor = None
    db_session.commit()
    _patch_job(monkeypatch)

    _draft(client, item.id)
    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft_status == "echec"
    assert "NOR" in stored.draft_error


def test_the_job_holds_no_database_session_while_it_waits(db_session, background_db, monkeypatch):
    """The long steps must not sit on a connection — nor on a snapshot.

    A ``Session`` that has read anything keeps its transaction open until it
    commits or closes, and this job then spends minutes on Journal Officiel
    archives and two model calls. Two of them at once used to leave two backends
    idle in transaction for as long as the deadline allows, which is enough to
    stop autovacuum reclaiming dead rows across the whole database — including
    ``votes``, the one table being written to continuously — and to make the
    ``ALTER TABLE`` of a redeploy queue behind a model call.
    """
    from app import drafting

    item = _stage(db_session)
    open_sessions = 0
    held_during: list[str] = []

    inner = drafting.SessionLocal

    def counting_session():
        nonlocal open_sessions
        db = inner()
        open_sessions += 1
        real_close = db.close

        def close():
            nonlocal open_sessions
            open_sessions -= 1
            real_close()

        db.close = close
        return db

    monkeypatch.setattr("app.drafting.SessionLocal", counting_session)

    def watch(step):
        if open_sessions:
            held_during.append(step)

    _patch_job(monkeypatch)
    for step, name in (
        ("app.drafting.fetch_law_text", "texte officiel"),
        ("app.drafting.generate_draft", "rédaction"),
        ("app.drafting.fetch_speeches", "comptes rendus"),
        ("app.drafting.select_arguments", "sélection des arguments"),
    ):
        previous = getattr(drafting, step.rsplit(".", 1)[1])
        monkeypatch.setattr(
            step,
            lambda *a, _p=previous, _n=name, **k: (watch(_n), _p(*a, **k))[1],
        )

    drafting.build_draft(item.id)

    assert not held_during, f"a session was open during: {held_during}"
    assert open_sessions == 0, "the job left a session open"
    db_session.expire_all()
    assert db_session.get(ImportedLaw, item.id).draft_status == "pret"


def test_the_job_gives_up_rather_than_hanging(db_session, background_db, monkeypatch):
    """A whole-job deadline keeps a slow source from pinning a worker forever."""
    from app import drafting

    item = _stage(db_session)
    monkeypatch.setattr(get_settings(), "ai_draft_deadline_seconds", 0, raising=False)
    _patch_job(monkeypatch)

    drafting.build_draft(item.id)

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft_status == "echec"
    assert "délai dépassé" in stored.draft_error


def test_draft_route_requires_auth(client, db_session):
    item = _stage(db_session)
    assert client.post(f"/admin/imports/{item.id}/brouillon").status_code == 401


def test_draft_route_404_for_unknown_import(client, db_session):
    assert client.post("/admin/imports/999/brouillon", headers=_admin_auth()).status_code == 404


# --- The AI corpus is measured, not just trimmed ------------------------------


def test_an_oversized_debate_corpus_is_refused_before_it_is_billed():
    """`generate_draft` measured its input; `select_arguments` only guessed."""
    from app.ai import AiDraftError, select_arguments
    from app.debates import Speech
    from app.legifrance import LawText

    class _Client:
        class messages:  # mirrors the SDK's client.messages.* shape
            @staticmethod
            def count_tokens(**_kw):
                return type("R", (), {"input_tokens": 10_000_000})()

            @staticmethod
            def parse(**_kw):  # pragma: no cover - must never be reached
                raise AssertionError("the model was called despite the size guard")

    law = LawText(
        nor="ABCX2600001L",
        law_code="2026-1",
        title="Loi",
        nature="LOI",
        signed_on=None,
        eli_url=None,
    )
    speeches = [
        Speech(
            speech_id="s1",
            speaker="Mme A",
            quality="",
            chamber="Assemblée nationale",
            section="Discussion générale",
            spoken_on=None,
            text="x" * 500,
            url="https://an.fr/cr",
        )
    ]
    with pytest.raises(AiDraftError, match="trop volumineux"):
        select_arguments(law, speeches, client=_Client())


# --- A draft the process did not live to finish -------------------------------


def test_a_draft_interrupted_by_a_restart_is_released(client, db_session):
    """`en_cours` means "a thread here is on it", and no thread outlives the process.

    A redeploy cancels what is queued and a crash takes it with it, leaving rows
    claiming a run nobody was performing — with every exit closed: the banner
    polls for an outcome that will never be written, « masquer » only clears a
    *failed* draft, and « Régénérer » refuses to reschedule a row it believes is
    already running.
    """
    item = _stage(db_session)
    imports_crud.mark_draft_pending(db_session, item)
    assert item.draft_status == DRAFT_PENDING

    released = imports_crud.release_interrupted_drafts(db_session)

    assert released == 1
    db_session.refresh(item)
    assert item.draft_status == DRAFT_FAILED
    assert item.draft_error == imports_crud.INTERRUPTED_MESSAGE

    # And now the administrator can act on it again: the banner is dismissible…
    r = client.post(f"/admin/imports/{item.id}/brouillon/masquer", headers=_admin_auth())
    assert r.status_code in (200, 303)
    db_session.refresh(item)
    assert item.draft_status != DRAFT_PENDING


def test_releasing_leaves_finished_drafts_alone(db_session):
    """Only a run that was in flight is a run that was interrupted."""
    ready = _stage(db_session, uid="DLR5L17N901")
    imports_crud.save_draft(db_session, ready, draft={"summary_text": "x"})
    idle = _stage(db_session, uid="DLR5L17N902")

    assert imports_crud.release_interrupted_drafts(db_session) == 0

    db_session.refresh(ready)
    db_session.refresh(idle)
    assert ready.draft_status == DRAFT_READY
    assert idle.draft_status == DRAFT_IDLE


def test_the_draft_claim_is_won_once(db_session):
    """Two clicks in the same second must schedule — and bill — exactly one run."""
    item = _stage(db_session)

    assert imports_crud.mark_draft_pending(db_session, item) is True
    assert imports_crud.mark_draft_pending(db_session, item) is False, (
        "a row already running must not be claimed a second time"
    )
    assert item.draft_status == DRAFT_PENDING


def test_a_second_click_does_not_queue_a_second_job(client, db_session, background_db, monkeypatch):
    """The route's gate is the claim, so the queue never grows a duplicate."""
    calls: list[int] = []
    _patch_job(monkeypatch, calls=calls)
    item = _stage(db_session)

    for _ in range(3):
        client.post(
            f"/admin/imports/{item.id}/brouillon", headers=_admin_auth(), data={"regenerer": "1"}
        )
    drain_drafts()

    assert len(calls) == 1, f"the model was asked {len(calls)} times for one draft"


# --- Drafting stays reachable once the law exists -----------------------------


def _imported_law(db_session, uid="DLR5L17N910"):
    """A law created from a staged import, linked as the create route links them."""
    item = _stage(db_session, uid=uid)
    imports_crud.save_draft(
        db_session,
        item,
        draft={
            "summary_text": "Résumé proposé par le modèle.",
            "practical_changes": "Ce que cela change, selon le modèle.",
            "arguments_for": [{"text": "Pour, cité.", "author": "Mme A", "source": ""}],
            "arguments_against": [],
            "notes": [],
            "eli_url": "",
        },
    )
    law = make_law(
        db_session,
        slug="loi-importee",
        title="Titre enregistré",
        summary_text="Texte enregistré.",
        published=False,
    )
    imports_crud.set_status(db_session, item, status=STATUS_IMPORTED, law_id=law.id)
    return item, law


def test_the_edit_form_of_an_imported_law_still_offers_the_draft(client, db_session):
    """It offered nothing at all: the AI actions were on the creation form only.

    So a presentation could be drafted exactly once, before the law existed, and
    never again — « Régénérer » included. The cached draft, already paid for,
    became unreachable the moment the law was saved.
    """
    _item, law = _imported_law(db_session)
    settings, previous = _with_key("sk-test")
    try:
        page = client.get(f"/admin/lois/{law.id}", headers=_admin_auth()).text
    finally:
        settings.anthropic_api_key = previous

    assert "Régénérer" in page
    assert "Charger le brouillon dans le formulaire" in page


def test_a_law_that_came_from_nowhere_offers_no_draft(client, db_session):
    """Only a law with a staged row behind it has anything to redraft from."""
    law = make_law(db_session, slug="loi-manuelle", title="Saisie à la main")
    settings, previous = _with_key("sk-test")
    try:
        page = client.get(f"/admin/lois/{law.id}", headers=_admin_auth()).text
    finally:
        settings.anthropic_api_key = previous
    assert "Régénérer" not in page
    assert "brouillon" not in page.lower()


def test_loading_the_draft_fills_the_blocks_and_keeps_the_rest(client, db_session):
    """The draft has nothing to say about a title or a voting window.

    Overwriting those with its silence would turn "show me the draft" into "lose
    my schedule".
    """
    _item, law = _imported_law(db_session)
    page = client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth()).text

    assert escape("Résumé proposé par le modèle.") in page, "the draft is not shown"
    assert "Texte enregistré." not in page, "the stored text should be replaced in the field"
    assert escape("Titre enregistré") in page, "the title must survive"
    # And it is still the *edit* form: saving must not create a second law.
    assert f'action="/admin/lois/{law.id}"' in page


def test_loading_the_draft_writes_nothing(client, db_session):
    """Looking is not saving; « Enregistrer » is."""
    _item, law = _imported_law(db_session)
    client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth())

    db_session.expire_all()
    stored = db_session.get(Law, law.id)
    assert stored.summary_text == "Texte enregistré."
    assert stored.title == "Titre enregistré"


def test_the_edit_form_says_the_draft_is_not_saved_yet(client, db_session):
    _item, law = _imported_law(db_session)
    page = client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth()).text
    # Not `escape()`: this is literal text in the template, which Jinja emits
    # verbatim. Only `{{ ... }}` expressions are escaped on the way out.
    assert "Rien n'est enregistré" in page


def test_without_javascript_drafting_returns_to_the_law_it_belongs_to(
    client, db_session, background_db, monkeypatch
):
    """The plain POST used to answer with the *creation* form for an existing law.

    Its « Enregistrer » posts to /admin/lois, so following it made a second copy
    of a law that already existed.
    """
    _patch_job(monkeypatch)
    item, law = _imported_law(db_session)

    r = client.post(
        f"/admin/imports/{item.id}/brouillon",
        headers=_admin_auth(),
        data={"regenerer": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/lois/{law.id}"


def test_the_ready_banner_points_at_the_law_when_there_is_one(client, db_session):
    """« Le brouillon est prêt » must not send you to the *creation* form.

    This banner was written for the creation flow and always linked there. Once
    it started appearing on the edit form too, following it after « Régénérer »
    gave you a blank creation form holding the new draft — and saving that made a
    second copy of the law you were editing, while re-pointing the import at the
    copy.
    """
    item, law = _imported_law(db_session)
    banner = client.get(f"/admin/imports/{item.id}/brouillon/etat", headers=_admin_auth()).text

    assert f"/admin/lois/{law.id}?brouillon=1" in banner
    assert "/admin/lois/nouvelle" not in banner, "the creation form is the wrong door here"


def test_the_ready_banner_still_points_at_creation_before_a_law_exists(client, db_session):
    item = _stage(db_session, uid="DLR5L17N911")
    imports_crud.save_draft(db_session, item, draft={"summary_text": "x"})
    banner = client.get(f"/admin/imports/{item.id}/brouillon/etat", headers=_admin_auth()).text
    assert f"/admin/lois/nouvelle?import_id={item.id}" in banner


def test_one_dossier_cannot_produce_a_second_votation(client, db_session):
    """The guard behind the link: the route must refuse on its own.

    Creating a second law from a staged row that already has one does not merely
    duplicate it — `set_status` re-points the import at the newcomer, so the
    original silently loses the link to its own draft.
    """
    item, law = _imported_law(db_session)
    before = db_session.query(Law).count()

    r = client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Une seconde copie", "summary_text": "x", "import_id": item.id},
        follow_redirects=False,
    )

    assert r.status_code == 409
    assert db_session.query(Law).count() == before, "a duplicate was created anyway"
    db_session.expire_all()
    assert db_session.get(ImportedLaw, item.id).law_id == law.id, "the import was re-pointed"


def test_a_dossier_without_a_law_still_creates_one(client, db_session):
    """The guard must not block the ordinary path it was added to protect."""
    item = _stage(db_session, uid="DLR5L17N912")
    r = client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Première votation", "summary_text": "x", "import_id": item.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire_all()
    created = db_session.query(Law).filter_by(title="Première votation").one()
    assert db_session.get(ImportedLaw, item.id).law_id == created.id
