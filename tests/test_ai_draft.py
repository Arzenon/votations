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
    """Fake anthropic.Anthropic that records its calls."""

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
    """The neutrality rules are in the prompt."""
    system = ai.SYSTEM_PROMPT
    assert "N'invente aucun fait" in system
    assert "Aucune prise de position" in system
    # Arguments are out of scope: the JO carries no attributable positions.
    assert "arguments" not in system.lower()


def test_the_second_block_is_asked_for_something_the_first_cannot_say():
    """The second block answers a different question, not the first one rephrased.

    Asking only for simpler wording gave a second block that repeated all the
    facts of the first.
    """
    system = ai.SYSTEM_PROMPT
    assert "pas le premier bloc" in system, "the restatement must be forbidden outright"
    assert "N'y répète pas ce qui est déjà dans summary_text" in system
    assert "laisse ce bloc vide" in system, "an empty block must be an allowed answer"
    # Empty only when the law creates no obligation. A looser rule once gave an
    # empty block on a 61-article agriculture law.
    assert "Ne laisse ce bloc vide que si la loi ne crée d'obligation" in system
    assert "il est à écrire" in system, "an empty block must not be reachable by attrition"
    # On overlap, block 1 gets shorter rather than block 2 empty.
    assert "elle appartient au \\\n second" in system or "elle appartient au" in system
    # Framed by who is affected, rather than by the shape of the text.
    assert "personnes concernées" in system
    assert "ne change pas" in system, "what stays the same is information too"


def test_the_two_blocks_are_told_to_divide_the_work_both_ways():
    """The no-repeat rule applies both ways.

    Block 1 describes the law from outside and leaves obligations to block 2.
    """
    system = ai.SYSTEM_PROMPT
    assert "l'idée de la loi, pas son plan" in system
    assert "N'y détaille pas les obligations" in system, "block 1 must defer the duties"
    assert "Les deux blocs ne se recouvrent pas" in system
    # A law with no obligations is presented in block 1 alone.
    assert "sera vide" in system


def test_the_second_block_is_held_short_per_audience_not_capped_in_audiences():
    """Length is limited per paragraph, not by capping the number of audiences.

    Capping the count would mean choosing who goes unmentioned. Each
    paragraph stays short but precise.
    """
    system = ai.SYSTEM_PROMPT
    assert "Un paragraphe court par public concerné" in system
    assert "n'est pas plafonné" in system, "the number of audiences must stay free"
    # The count follows readers, not provisions (see the granularity test).
    assert "nombre de **lecteurs** concernés" in system

    # The limit must not be followed by caveats: they made paragraphs longer
    # (540 to 747 characters on average).
    for undercut in (
        "chacun portant beaucoup",
        "jamais pour te faire rester vague",
        "Trois faits exacts valent mieux",
    ):
        assert undercut not in system, f"the cap is undercut again by: {undercut}"
    # One anti-vagueness clause remains.
    assert "va au texte officiel" not in system
    assert "sans jamais remplacer un fait précis par une formule générale" in system


def test_the_prompt_says_each_thing_once():
    """Each rule is stated once: duplicated rules drift and the loosest one wins."""
    system = ai.SYSTEM_PROMPT
    # The loose licence is gone; the precise one remains.
    assert "laisse-le vide plutôt que de le combler" not in system
    assert "Ne laisse ce bloc vide que si la loi ne crée d'obligation" in system
    # The ratification case appears twice, with a different consequence each time.
    assert system.count("ratification d'un accord") == 2
    assert "Si la loi se borne à ratifier un accord international" not in system
    # Block 1 no longer asks for "par quels moyens elle agit", which contradicted
    # the next rule.
    assert "par quels moyens elle agit" not in system
    assert "Nomme le domaine sur lequel la loi intervient" in system


def test_an_audience_is_how_someone_describes_themselves():
    """An audience is how people describe themselves ("agriculteur", not "irrigant").

    Details like thresholds go inside the paragraph.
    """
    system = ai.SYSTEM_PROMPT
    assert "façon dont quelqu'un se décrit lui-même" in system
    assert "ça, c'est moi" in system, "the test the model applies must be the reader's"
    # Grouping, never dropping.
    assert "Ne supprime jamais la mesure" in system
    assert "l'écarter parce que peu de gens sont concernés, jamais" in system
    # Largest audiences first.
    assert "Commence par les publics les plus nombreux" in system


def test_the_first_block_is_written_for_someone_who_never_opened_a_code():
    """Block 1 has no article numbers and no lists of codes."""
    system = ai.SYSTEM_PROMPT
    assert "Ne cite aucun numéro d'article ni de code" in system
    assert "N'énumère pas les codes modifiés" in system
    # What was censured is still mentioned, just not the article numbers.
    assert "Dis *ce que* le Conseil" in system


def test_the_first_block_may_still_describe_how_power_is_allocated():
    """Block 1 may still describe who decides what.

    A rule against "administrative mechanics" once turned "la prorogation
    au-delà de deux mois doit être autorisée par la loi" into "sous contrôle
    du Parlement", which takes a side in the debate.
    """
    system = ai.SYSTEM_PROMPT
    assert "mécanique administrative" not in system
    assert "Aucune prise de position" in system


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
    """The draft button is on the law form."""
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
    """The form shows both source links."""
    item = _stage(db_session)
    r = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
    assert item.dossier_url in r.text
    assert "Dossier Assemblée nationale" in r.text
    assert "Texte sur Légifrance" in r.text
    assert "LOI n° 2026-668" in r.text


def _patch_job(monkeypatch, *, draft=DRAFT, calls=None, fail=None, arguments=None, speeches=("s",)):
    """Replace the network calls of the drafting job with fakes."""

    # Same signature as the real functions, including the getter.
    def fetch(nor, jo_date, get=None):
        if fail:
            raise fail
        return LAW

    def generate(law, *, client=None):
        if calls is not None:
            calls.append(1)
        return draft

    monkeypatch.setattr("app.drafting.fetch_law_text", fetch)
    monkeypatch.setattr("app.drafting.generate_draft", generate)
    monkeypatch.setattr("app.drafting.fetch_speeches", lambda seances, get=None: list(speeches))
    if arguments is not None:
        monkeypatch.setattr("app.drafting.select_arguments", lambda law, sp, client=None: arguments)


def _draft(client, item_id, **data):
    """Request a draft, then run the queued job.

    The response is what the admin sees while the job is pending.
    """
    response = client.post(
        f"/admin/imports/{item_id}/brouillon", headers=_admin_auth(), data=data or None
    )
    drain_drafts()
    return response


def _form(client, item_id):
    return client.get(f"/admin/lois/nouvelle?import_id={item_id}", headers=_admin_auth())


def test_drafting_is_scheduled_not_awaited(client, db_session, background_db, monkeypatch):
    """The draft is scheduled, not run during the request."""
    item = _stage(db_session)
    _patch_job(monkeypatch)

    r = _draft(client, item.id)
    assert r.status_code == 200
    # Immediate response, showing the job in progress...
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
    # Finished: no more polling.
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


def test_the_creation_form_prefills_the_promulgation_date(client, db_session):
    """The promulgation date is pre-filled from the staged row."""
    item = _stage(db_session)
    form = _form(client, item.id).text
    assert 'name="promulgated_on"' in form
    assert 'value="2026-07-27"' in form


def test_opening_the_form_shows_a_draft_already_paid_for(
    client, db_session, background_db, monkeypatch
):
    """An existing draft is shown right away."""
    item = _stage(db_session)
    _patch_job(monkeypatch)
    # The button only shows with a key; don't depend on the developer's .env.
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-test")
    _draft(client, item.id)

    r = _form(client, item.id)
    assert str(escape(DRAFT.summary_text)) in r.text
    assert "Revoir le brouillon IA" in r.text
    assert "Régénérer" in r.text


def test_a_cached_draft_is_reused_without_calling_the_model(
    client, db_session, background_db, monkeypatch
):
    """A stored draft is reused without calling the model."""
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
    """The stored draft survives a restart (it's in the database)."""
    item = _stage(db_session)
    _patch_job(monkeypatch)
    _draft(client, item.id)

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft["practical_changes"] == DRAFT.practical_changes

    # No model available: the draft must still be shown.
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

    # Still true after the job: the draft only fills empty blocks.
    again = _draft(client, item.id, summary_text=mine, title="Mon titre")
    assert str(escape(mine)) in again.text
    assert str(escape(DRAFT.practical_changes)) in again.text


def test_the_published_checkbox_survives_a_draft(client, db_session, background_db, monkeypatch):
    """Requesting a draft keeps the published checkbox."""
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
    """No session is held open during the slow steps (idle transactions)."""
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
    """The job gives up when its deadline passes."""
    from app import drafting

    item = _stage(db_session)
    monkeypatch.setattr(get_settings(), "ai_draft_deadline_seconds", 0, raising=False)
    _patch_job(monkeypatch)

    drafting.build_draft(item.id)

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft_status == "echec"
    assert "délai dépassé" in stored.draft_error


def _deadline_of(get) -> float | None:
    """The deadline this getter would pass to a request, or None.

    Restores by hand: monkeypatch.undo() would also revert the job's fakes.
    """
    from contextlib import suppress

    from app import opendata

    if get is None:
        return None  # no getter: unbounded
    captured: list[float | None] = []

    def spy(url, **kwargs):
        captured.append(kwargs.get("deadline"))
        raise _Stop

    real, opendata.fetch = opendata.fetch, spy
    try:
        with suppress(_Stop):
            get("https://example.invalid/x")
    finally:
        opendata.fetch = real
    return captured[0] if captured else None


class _Stop(Exception):
    """Stops a fetch once its arguments are captured."""


def test_the_budget_reaches_the_downloads_themselves(db_session, background_db, monkeypatch):
    """Each download gets the remaining budget as its deadline."""
    from app import drafting
    from app.ai import SelectedArguments

    item = _stage(db_session)
    # Needs a sitting, otherwise the arguments step is skipped.
    item.seances = [{"reunion_ref": "RUANR5L17S2026PO1", "odj_ref": "ODJ1", "date": "2026-07-01"}]
    db_session.commit()
    monkeypatch.setattr(get_settings(), "ai_draft_deadline_seconds", 600, raising=False)

    seen: dict[str, float | None] = {}

    def fetch(nor, jo_date, get=None):
        seen["texte officiel"] = _deadline_of(get)
        return LAW

    def speeches(seances, get=None):
        seen["comptes rendus"] = _deadline_of(get)
        return ["s"]

    monkeypatch.setattr("app.drafting.fetch_law_text", fetch)
    monkeypatch.setattr("app.drafting.fetch_speeches", speeches)
    monkeypatch.setattr("app.drafting.generate_draft", lambda law, *, client=None: DRAFT)
    monkeypatch.setattr(
        "app.drafting.select_arguments", lambda law, sp, client=None: SelectedArguments()
    )

    drafting.build_draft(item.id)

    for step in ("texte officiel", "comptes rendus"):
        budget = seen.get(step)
        assert budget is not None, f"downloads for {step!r} are not bounded"
        assert 0 < budget <= 600, f"{step!r} got {budget} s out of a 600 s budget"


def test_a_download_that_never_ends_is_cut_at_the_budget(monkeypatch):
    """A server sending one byte at a time is cut off at the deadline.

    httpx's timeout is per read, so it never fires in that case.
    """
    import time
    from contextlib import contextmanager

    from app import opendata

    class _Dribble:
        def raise_for_status(self):
            pass

        def iter_bytes(self, size):
            while True:
                time.sleep(0.01)
                yield b"x"

    @contextmanager
    def never_ending(*args, **kwargs):
        yield _Dribble()

    monkeypatch.setattr(opendata.httpx, "stream", never_ending)

    started = time.monotonic()
    with pytest.raises(opendata.DeadlineExceeded, match="trop lent"):
        opendata.fetch(
            "https://example.invalid/lent",
            timeout=60.0,
            max_bytes=10**9,
            error=RuntimeError,
            deadline=0.2,
        )
    assert time.monotonic() - started < 5, "it was cut at the budget, not at the timeout"


def test_a_spent_budget_refuses_before_opening_a_connection(monkeypatch):
    """A spent budget raises DeadlineExceeded, not the source's own error.

    `fetch_speeches` skips a sitting on its own error but stops on this one.
    """
    from app import opendata

    def never_called(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a connection was opened on a spent budget")

    monkeypatch.setattr(opendata.httpx, "stream", never_called)
    with pytest.raises(opendata.DeadlineExceeded):
        opendata.fetch(
            "https://example.invalid/x",
            timeout=60.0,
            max_bytes=1000,
            error=RuntimeError,
            deadline=0,
        )


def test_draft_route_requires_auth(client, db_session):
    item = _stage(db_session)
    assert client.post(f"/admin/imports/{item.id}/brouillon").status_code == 401


def test_draft_route_404_for_unknown_import(client, db_session):
    assert client.post("/admin/imports/999/brouillon", headers=_admin_auth()).status_code == 404


# --- The AI input is measured, not just trimmed --------------------------------


def test_an_oversized_debate_corpus_is_refused_before_it_is_billed():
    """An oversized debate corpus is refused before calling the model."""
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
    """A draft left "en cours" by a restart is marked as failed at startup.

    Otherwise the row would be stuck: nothing in the UI can clear it.
    """
    item = _stage(db_session)
    imports_crud.mark_draft_pending(db_session, item)
    assert item.draft_status == DRAFT_PENDING

    released = imports_crud.release_interrupted_drafts(db_session)

    assert released == 1
    db_session.refresh(item)
    assert item.draft_status == DRAFT_FAILED
    assert item.draft_error == imports_crud.INTERRUPTED_MESSAGE

    # The banner can be dismissed again...
    r = client.post(f"/admin/imports/{item.id}/brouillon/masquer", headers=_admin_auth())
    assert r.status_code in (200, 303)
    db_session.refresh(item)
    assert item.draft_status != DRAFT_PENDING


def test_releasing_leaves_finished_drafts_alone(db_session):
    """Finished drafts aren't touched."""
    ready = _stage(db_session, uid="DLR5L17N901")
    imports_crud.save_draft(db_session, ready, draft={"summary_text": "x"})
    idle = _stage(db_session, uid="DLR5L17N902")

    assert imports_crud.release_interrupted_drafts(db_session) == 0

    db_session.refresh(ready)
    db_session.refresh(idle)
    assert ready.draft_status == DRAFT_READY
    assert idle.draft_status == DRAFT_IDLE


def test_the_draft_claim_is_won_once(db_session):
    """Two simultaneous clicks schedule a single run."""
    item = _stage(db_session)

    assert imports_crud.mark_draft_pending(db_session, item) is True
    assert imports_crud.mark_draft_pending(db_session, item) is False, (
        "a row already running must not be claimed a second time"
    )
    assert item.draft_status == DRAFT_PENDING


def test_a_second_click_does_not_queue_a_second_job(client, db_session, background_db, monkeypatch):
    """A second click doesn't queue a second job."""
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
    """A law created from a staged import, linked like the create route does."""
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
    item.law_id = law.id
    imports_crud.set_status(db_session, item, status=STATUS_IMPORTED)
    return item, law


def test_the_edit_form_of_an_imported_law_still_offers_the_draft(client, db_session):
    """The edit form of an imported law offers the draft too."""
    _item, law = _imported_law(db_session)
    settings, previous = _with_key("sk-test")
    try:
        page = client.get(f"/admin/lois/{law.id}", headers=_admin_auth()).text
    finally:
        settings.anthropic_api_key = previous

    assert "Régénérer" in page
    assert "Charger le brouillon dans le formulaire" in page


def test_a_law_that_came_from_nowhere_offers_no_draft(client, db_session):
    """A law without a staged row has no draft."""
    law = make_law(db_session, slug="loi-manuelle", title="Saisie à la main")
    settings, previous = _with_key("sk-test")
    try:
        page = client.get(f"/admin/lois/{law.id}", headers=_admin_auth()).text
    finally:
        settings.anthropic_api_key = previous
    assert "Régénérer" not in page
    assert "brouillon" not in page.lower()


def test_loading_the_draft_fills_the_blocks_and_keeps_the_rest(client, db_session):
    """Loading the draft fills the text blocks and keeps the other fields."""
    _item, law = _imported_law(db_session)
    page = client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth()).text

    assert escape("Résumé proposé par le modèle.") in page, "the draft is not shown"
    assert "Texte enregistré." not in page, "the stored text should be replaced in the field"
    assert escape("Titre enregistré") in page, "the title must survive"
    # Still the edit form, so saving won't create a second law.
    assert f'action="/admin/lois/{law.id}"' in page


def test_loading_the_draft_writes_nothing(client, db_session):
    """Loading the draft doesn't save anything."""
    _item, law = _imported_law(db_session)
    client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth())

    db_session.expire_all()
    stored = db_session.get(Law, law.id)
    assert stored.summary_text == "Texte enregistré."
    assert stored.title == "Titre enregistré"


def test_the_edit_form_says_the_draft_is_not_saved_yet(client, db_session):
    _item, law = _imported_law(db_session)
    page = client.get(f"/admin/lois/{law.id}?brouillon=1", headers=_admin_auth()).text
    # Literal template text, so not escaped.
    assert "Rien n'est enregistré" in page


def test_without_javascript_drafting_returns_to_the_law_it_belongs_to(
    client, db_session, background_db, monkeypatch
):
    """Without JS, drafting for an existing law redirects to its edit form.

    The creation form would create a duplicate.
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
    """The "brouillon prêt" banner links to the existing law, not the creation form."""
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
    """Creating a second law from the same dossier is refused."""
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
    """A dossier without a law can still create one."""
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
