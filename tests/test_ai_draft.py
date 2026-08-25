"""AI-assisted drafting of the two factual presentation blocks."""

import base64
from datetime import date

import pytest
from markupsafe import escape

from app import ai
from app.ai import AiDraftError, LawDraft, SelectedArguments, generate_draft
from app.config import get_settings
from app.crud import imports as imports_crud
from app.legifrance import Article, LawText
from app.models import (
    DRAFT_FAILED,
    DRAFT_IDLE,
    DRAFT_PENDING,
    DRAFT_READY,
    ImportedLaw,
)
from tests.conftest import drain_drafts


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
        monkeypatch.setattr(
            "app.drafting.select_arguments", lambda law, sp, client=None: arguments
        )


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


def test_reserves_are_shown_apart_and_never_prefilled(
    client, db_session, background_db, monkeypatch
):
    """A supporter's misgiving is information, not an argument against."""
    item = _stage(db_session)
    # Arguments are only sought when the text was actually debated.
    item.seances = [{"reunion_ref": "RU1", "odj_ref": "RU1PT1"}]
    db_session.commit()
    reserve = {
        "text": "Nous voterons ce texte, mais il manque les moyens de l'appliquer.",
        "author": "M. Hendrik Davi, séance du 08/04/2026 · Assemblée nationale",
        "source": "https://an.fr/cr#9",
    }
    _patch_job(
        monkeypatch,
        arguments=SelectedArguments(
            pour=[{"text": "Un argument pour.", "author": "Mme X", "source": "u"}],
            contre=[],
            reserves=[reserve],
            notes=["Aucune opposition au texte dans les débats."],
        ),
    )

    _draft(client, item.id)
    r = _form(client, item.id)
    assert r.status_code == 200
    assert "Réserves exprimées" in r.text
    assert str(escape(reserve["text"])) in r.text
    assert "Hendrik Davi" in r.text
    # Shown for information — never written into the opposition block.
    against = r.text.split('name="arguments_against"')[1].split("</textarea>")[0]
    assert "manque les moyens" not in against
    assert against.strip().endswith("[]")

    # And it survives the cache, like the rest of the draft.
    db_session.expire_all()
    assert db_session.get(ImportedLaw, item.id).draft["reserves"] == [reserve]
    assert "Réserves exprimées" in _form(client, item.id).text


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
    r = client.post(
        f"/admin/imports/{item.id}/brouillon/masquer", headers=_admin_auth()
    )
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


def test_a_second_click_does_not_queue_a_second_job(
    client, db_session, background_db, monkeypatch
):
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
