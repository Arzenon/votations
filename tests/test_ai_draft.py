"""AI-assisted drafting of the two factual presentation blocks."""

import base64
from datetime import date

from markupsafe import escape

from app import ai
from app.ai import AiDraftError, LawDraft, SelectedArguments, generate_draft
from app.config import get_settings
from app.crud import imports as imports_crud
from app.legifrance import Article, LawText
from app.models import ImportedLaw


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
    published_on=date(2026, 7, 28),
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
        nor="X", law_code="1", title="t", nature="LOI",
        signed_on=None, published_on=None, eli_url=None, articles=[],
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


def _stage(db_session) -> ImportedLaw:
    item = ImportedLaw(
        source="assemblee", dossier_uid="DLR5L17N001", legislature="17",
        procedure="Proposition de loi ordinaire", title="Loi de test",
        law_title="portant diverses dispositions", law_code="2026-668",
        promulgated_on=date(2026, 7, 27), jo_date=date(2026, 7, 28),
        nor="SFHX2609544L", legifrance_url="http://legifrance.gouv.fr/legacy",
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


def test_draft_route_renders_the_form_and_caches_the_result(client, db_session, monkeypatch):
    item = _stage(db_session)
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr("app.routes.admin.generate_draft", lambda law: DRAFT)

    r = client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
    assert r.status_code == 200
    # The draft lands in the editable form, HTML-escaped (never marked |safe).
    assert str(escape(DRAFT.summary_text)) in r.text
    assert str(escape(DRAFT.practical_changes)) in r.text
    assert "d&#39;un" in r.text
    assert "Brouillon généré par IA" in r.text
    assert f'name="import_id" value="{item.id}"' in r.text
    # The durable ELI link is preferred over the legacy one.
    assert LAW.eli_url in r.text
    assert "legifrance.gouv.fr/legacy" not in r.text

    # Stored so reopening never bills again — but still not a published law.
    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft["summary_text"] == DRAFT.summary_text
    assert stored.drafted_at is not None
    assert stored.status == "nouveau"
    assert stored.law_id is None


def test_opening_the_form_shows_a_draft_already_paid_for(client, db_session, monkeypatch):
    """No extra click to see work that has already been billed."""
    item = _stage(db_session)
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr("app.routes.admin.generate_draft", lambda law: DRAFT)
    client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())

    r = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
    assert str(escape(DRAFT.summary_text)) in r.text
    assert "Revoir le brouillon IA" in r.text
    assert "Régénérer" in r.text


def test_a_cached_draft_is_reused_without_calling_the_model(client, db_session, monkeypatch):
    """Generating is billable; reopening must be free."""
    item = _stage(db_session)
    calls = []
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr(
        "app.routes.admin.generate_draft", lambda law: (calls.append(1), DRAFT)[1]
    )

    client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
    assert len(calls) == 1

    for _ in range(3):
        r = client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
        assert str(escape(DRAFT.summary_text)) in r.text
    assert len(calls) == 1, "reopening a draft must not call the model again"


def test_a_cached_draft_survives_a_restart(client, db_session, monkeypatch):
    """The cache is a database column, not process state."""
    item = _stage(db_session)
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr("app.routes.admin.generate_draft", lambda law: DRAFT)
    client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.draft["practical_changes"] == DRAFT.practical_changes

    # No model available at all now — the draft must still come back.
    def gone(law):
        raise AssertionError("must not call the model for a cached draft")

    monkeypatch.setattr("app.routes.admin.generate_draft", gone)
    r = client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
    assert str(escape(DRAFT.summary_text)) in r.text


def test_regenerating_is_explicit(client, db_session, monkeypatch):
    item = _stage(db_session)
    calls = []
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr(
        "app.routes.admin.generate_draft", lambda law: (calls.append(1), DRAFT)[1]
    )

    client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
    client.post(
        f"/admin/imports/{item.id}/brouillon", headers=_admin_auth(), data={"regenerer": "1"}
    )
    assert len(calls) == 2


def test_reserves_are_shown_apart_and_never_prefilled(client, db_session, monkeypatch):
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
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr("app.routes.admin.generate_draft", lambda law: DRAFT)
    monkeypatch.setattr("app.routes.admin.fetch_speeches", lambda seances: ["s"])
    monkeypatch.setattr(
        "app.routes.admin.select_arguments",
        lambda law, sp: SelectedArguments(
            pour=[{"text": "Un argument pour.", "author": "Mme X", "source": "u"}],
            contre=[],
            reserves=[reserve],
            notes=["Aucune opposition au texte dans les débats."],
        ),
    )

    r = client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
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
    again = client.post(f"/admin/imports/{item.id}/brouillon", headers=_admin_auth())
    assert "Réserves exprimées" in again.text


def test_a_draft_never_overwrites_what_the_admin_typed(client, db_session, monkeypatch):
    item = _stage(db_session)
    monkeypatch.setattr("app.routes.admin.fetch_law_text", lambda nor, jo_date: LAW)
    monkeypatch.setattr("app.routes.admin.generate_draft", lambda law: DRAFT)

    mine = "Mon propre résumé, écrit à la main."
    r = client.post(
        f"/admin/imports/{item.id}/brouillon",
        headers=_admin_auth(),
        data={"summary_text": mine, "title": "Mon titre"},
    )
    assert str(escape(mine)) in r.text
    assert "Mon titre" in r.text
    # A block left empty is still filled by the draft.
    assert str(escape(DRAFT.practical_changes)) in r.text


def test_draft_failure_keeps_the_admin_on_the_form(client, db_session, monkeypatch):
    item = _stage(db_session)

    def boom(nor, jo_date):
        raise ai.AiDraftError("texte introuvable pour le NOR SFHX2609544L")

    monkeypatch.setattr("app.routes.admin.fetch_law_text", boom)
    mine = "Ce que j'avais déjà écrit."
    r = client.post(
        f"/admin/imports/{item.id}/brouillon",
        headers=_admin_auth(),
        data={"summary_text": mine},
    )
    assert r.status_code == 200
    assert "texte introuvable" in r.text
    # The admin stays on the form with their work intact.
    assert str(escape(mine)) in r.text
    assert imports_crud.count_new(db_session) == 1


def test_draft_route_requires_auth(client, db_session):
    item = _stage(db_session)
    assert client.post(f"/admin/imports/{item.id}/brouillon").status_code == 401


def test_draft_route_404_for_unknown_import(client, db_session):
    assert client.post("/admin/imports/999/brouillon", headers=_admin_auth()).status_code == 404
