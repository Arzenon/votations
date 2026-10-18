"""Administering a law: the public record of edits, archiving, purging, preview."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud.laws import REVISIONS_PAGE_SIZE
from app.models import ImportedLaw, Law, LawRevision, Vote
from app.routes.admin.forms import LawForm, dt_local
from app.routes.admin.laws import create_law
from tests.helpers import admin_headers, make_law

# --- Public history of edits after publication --------------------------------


def test_editing_a_published_law_is_recorded_and_shown(client, db_session):
    law = make_law(db_session, slug="loi-rev", summary_text="Version initiale.")
    client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": law.title, "summary_text": "Version corrigée.", "published": "1"},
        follow_redirects=False,
    )
    recorded = db_session.query(LawRevision).filter_by(law_id=law.id).all()
    fields = {r.field for r in recorded}
    assert "summary_text" in fields
    entry = next(r for r in recorded if r.field == "summary_text")
    assert entry.old_value == "Version initiale."
    assert entry.new_value == "Version corrigée."

    page = client.get(f"/loi/{law.slug}/resultats")
    assert "Version initiale." in page.text
    assert "Ce que dit la loi" in page.text


def test_editing_an_unpublished_draft_is_not_recorded(client, db_session):
    law = make_law(db_session, slug="loi-brouillon", published=False, summary_text="a")
    client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": law.title, "summary_text": "b"},
        follow_redirects=False,
    )
    assert db_session.query(LawRevision).filter_by(law_id=law.id).count() == 0


def test_an_unmodified_law_says_so_plainly(client, open_law):
    page = client.get(f"/loi/{open_law.slug}/resultats")
    assert "Aucune modification depuis la publication" in page.text


def test_the_law_page_points_at_the_history_when_there_is_one(client, db_session):
    law = make_law(db_session, slug="loi-modifiee", summary_text="a")
    laws_crud.update(db_session, law, summary_text="b")
    page = client.get(f"/loi/{law.slug}")
    assert "historique des modifications" in page.text.lower()


# --- Archiving instead of deletion --------------------------------------------


def test_archiving_hides_a_law_without_destroying_its_votes(client, db_session, open_law):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})

    r = client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    assert r.status_code == 303

    assert client.get(f"/loi/{open_law.slug}").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/resultats").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 404
    assert open_law.slug not in client.get("/").text

    db_session.refresh(open_law)
    assert open_law.archived_at is not None

    assert results_crud.count_votes(db_session, open_law.id) == 1, "votes are kept"


def test_an_archived_law_can_be_restored(client, db_session, open_law):
    client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    client.post(
        f"/admin/lois/{open_law.id}/restaurer", headers=admin_headers(), follow_redirects=False
    )
    assert client.get(f"/loi/{open_law.slug}").status_code == 200


def test_deleting_asks_on_a_page_the_server_renders(client, open_law):
    """Deletion is confirmed on a server-rendered page (works without JS)."""
    client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    page = client.get(f"/admin/lois/{open_law.id}/supprimer", headers=admin_headers())
    assert page.status_code == 200
    assert "<form" in page.text
    assert "irréversible" in page.text


def test_the_data_can_still_be_downloaded_from_the_page_that_destroys_it(
    client, db_session, open_law
):
    """The deletion page links to an export that works for archived laws."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )

    page = client.get(f"/admin/lois/{open_law.id}/supprimer", headers=admin_headers())
    offered = f"/admin/lois/{open_law.id}/export.csv"
    assert offered in page.text
    # The public route still refuses it.
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 404

    export = client.get(offered, headers=admin_headers())
    assert export.status_code == 200
    lines = export.text.strip().splitlines()
    assert lines[0] == "law_id,choice,created_at"
    assert len(lines) == 2, "the vote cast above should be in the file"


def test_a_law_cannot_be_purged_before_it_is_archived(client, db_session, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer", headers=admin_headers(), follow_redirects=False
    )
    assert r.status_code == 409
    assert db_session.get(Law, open_law.id) is not None


def test_purging_an_archived_law_removes_it_for_good(client, db_session, open_law):
    law_id = open_law.id
    client.post(f"/admin/lois/{law_id}/archiver", headers=admin_headers(), follow_redirects=False)
    r = client.post(
        f"/admin/lois/{law_id}/supprimer", headers=admin_headers(), follow_redirects=False
    )
    assert r.status_code == 303
    assert db_session.get(Law, law_id) is None


# --- Preview of an unpublished law ---------------------------------------------


def test_an_unpublished_law_can_be_previewed_by_the_admin(client, db_session):
    law = make_law(db_session, slug="loi-brouillon-apercu", title="Brouillon", published=False)
    assert client.get(f"/loi/{law.slug}").status_code == 404

    page = client.get(f"/admin/lois/{law.id}/apercu", headers=admin_headers())
    assert page.status_code == 200
    assert "Brouillon" in page.text
    assert "Aperçu" in page.text
    assert "n'est pas publiée" in page.text


def test_the_preview_shows_the_ballot_but_cannot_vote(client, db_session):
    law = make_law(db_session, slug="loi-apercu-vote", published=False)
    page = client.get(f"/admin/lois/{law.id}/apercu", headers=admin_headers()).text
    assert "Votre vote" in page, "the ballot should be shown as it will be"
    assert "disabled" in page

    # And the real vote route still refuses, preview or not.
    assert client.post(f"/loi/{law.slug}/vote", data={"choice": "for"}).status_code == 404


def test_the_preview_needs_the_admin_password(client, db_session):
    law = make_law(db_session, slug="loi-apercu-prive", published=False)
    assert client.get(f"/admin/lois/{law.id}/apercu").status_code == 401


def test_the_admin_import_states_are_french(client, db_session):
    from datetime import date

    from app.models import ImportedLaw

    db_session.add(
        ImportedLaw(
            source="assemblee",
            dossier_uid="D1",
            title="T",
            law_code="2026-1",
            promulgated_on=date(2026, 1, 1),
            dossier_url="https://an.fr",
        )
    )
    db_session.commit()
    r = client.get("/admin/imports", headers=admin_headers())
    assert "nouvelle" in r.text
    assert ">nouveau<" not in r.text


# --- Import review state ------------------------------------------------------


def test_ignoring_an_imported_row_keeps_its_link_to_the_law(db_session, open_law):
    from datetime import date

    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="D2",
        title="T",
        law_code="2026-2",
        promulgated_on=date(2026, 1, 1),
        dossier_url="https://an.fr",
    )
    db_session.add(item)
    db_session.commit()

    imports_crud.set_status(db_session, item, status="importe", law_id=open_law.id)
    assert item.law_id == open_law.id

    imports_crud.set_status(db_session, item, status="ignore")
    assert item.law_id == open_law.id, "ignoring a row must not orphan the law it produced"

    imports_crud.set_status(db_session, item, status="nouveau")
    assert item.law_id == open_law.id


# --- Import freshness ---------------------------------------------------------
#
# The dashboard warns when the import has stopped (e.g. the export URL changed).


def _stage_import(db_session, *, seen_at):
    from datetime import date

    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DFRESH",
        title="Loi importée",
        law_code="2026-9",
        promulgated_on=date(2026, 1, 1),
        dossier_url="https://an.fr",
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_a_fresh_import_shows_no_warning(client, db_session):
    _stage_import(db_session, seen_at=datetime.now(UTC) - timedelta(hours=1))
    page = client.get("/admin", headers=admin_headers()).text
    assert "Import automatique en retard" not in page


def test_a_stale_import_is_announced_on_the_dashboard(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "an_import_stale_after_hours", 48, raising=False)
    _stage_import(db_session, seen_at=datetime.now(UTC) - timedelta(hours=72))

    page = client.get("/admin", headers=admin_headers()).text
    assert "Import automatique en retard" in page
    # Names the service to check.
    assert "assemblee-refresh" in page


def test_never_having_imported_counts_as_stale(client, db_session):
    """No import ever counts as stale."""
    page = client.get("/admin", headers=admin_headers()).text
    assert "Import automatique en retard" in page
    assert "Aucun import n'a encore abouti" in page


# --- The public record of edits is paginated ----------------------------------


def _edit(client, law_id: int, summary: str) -> None:
    client.post(
        f"/admin/lois/{law_id}",
        headers=admin_headers(),
        data={"title": "Loi suivie", "summary_text": summary, "published": "1"},
        follow_redirects=False,
    )


def test_a_long_history_is_paginated_rather_than_shipped_whole(client, db_session):
    """The revision history is paginated.

    The count is read back: saving the form also clears the dates, so the
    first edit records several changes.
    """
    law = make_law(db_session, slug="loi-suivie", title="Loi suivie", published=True)
    for i in range(25):
        _edit(client, law.id, f"version {i}")
    total = db_session.query(LawRevision).filter_by(law_id=law.id).count()
    assert total > REVISIONS_PAGE_SIZE, "the point of the test is more than one page"

    first = client.get(f"/loi/{law.slug}/resultats")
    assert first.status_code == 200
    assert f"modifiée {total} fois" in first.text
    assert "version 24" in first.text, "the most recent edit is on the first page"
    assert "version 0" not in first.text, "the oldest must not be on the first page"
    assert "#historique" in first.text, "the page links carry the anchor"

    second = client.get(f"/loi/{law.slug}/resultats?revisions=2")
    assert "version 0" in second.text
    assert "version 24" not in second.text


def test_an_absurd_history_page_is_clamped(client, db_session):
    law = make_law(db_session, slug="loi-clamp", title="Loi clamp", published=True)
    _edit(client, law.id, "une seule modification")
    assert client.get(f"/loi/{law.slug}/resultats?revisions=9999").status_code == 200


def test_the_admin_form_says_it_is_showing_only_the_recent_ones(client, db_session):
    law = make_law(db_session, slug="loi-recentes", title="Loi récentes", published=True)
    for i in range(25):
        _edit(client, law.id, f"version {i}")
    total = db_session.query(LawRevision).filter_by(law_id=law.id).count()
    body = client.get(f"/admin/lois/{law.id}", headers=admin_headers()).text
    assert f"sur {total}" in body


# --- Previewing what is typed, not what is stored -----------------------------


def test_the_preview_renders_unsaved_text_and_saves_nothing(client, db_session):
    """The preview shows unsaved text and saves nothing."""
    before = db_session.query(Law).count()

    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={
            "title": "Titre jamais enregistré",
            "summary_text": "Paragraphe present uniquement dans le formulaire.",
        },
    )

    assert r.status_code == 200
    assert "Titre jamais enregistré" in r.text
    assert "Paragraphe present uniquement dans le formulaire." in r.text
    assert db_session.query(Law).count() == before, "the preview created a law"


def test_the_preview_route_is_not_swallowed_by_the_update_route(client, db_session):
    """POST /lois/apercu isn't caught by POST /lois/{law_id} (route order)."""
    r = client.post(
        "/admin/lois/apercu", headers=admin_headers(), data={"title": "T", "summary_text": "x"}
    )
    assert r.status_code == 200, "the literal route must be registered first"


def test_previewing_an_existing_law_keeps_its_identity(client, db_session):
    """Previewing an existing law keeps its tally, history and URL."""
    law = make_law(db_session, slug="loi-apercu", title="Titre enregistré")

    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={"law_id": law.id, "title": "Titre en cours de frappe", "summary_text": "x"},
    )

    assert "Titre en cours de frappe" in r.text
    assert f"/admin/lois/{law.id}" in r.text, "the way back to the form is lost"
    db_session.expire_all()
    assert db_session.get(Law, law.id).title == "Titre enregistré", "the preview wrote to the row"


def test_a_preview_of_an_unsaved_law_offers_no_dead_link(client, db_session):
    """No /admin/lois/None link in the preview of an unsaved law."""
    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={"title": "Sans id", "summary_text": "x"},
    )
    assert "/admin/lois/None" not in r.text
    assert "/resultats" not in r.text, "a results page that does not exist yet"


def test_the_preview_never_refuses_a_half_typed_form(client, db_session):
    """The preview accepts an incomplete form."""
    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={
            "title": "",  # the save route refuses this outright
            "arguments_for": "[{bracket manquante",
            "opens_at": "pas une date",
        },
    )
    assert r.status_code == 200


def test_the_preview_reads_the_dates_so_the_ballot_is_right(client, db_session):
    """The preview parses the dates, which decide what the page shows."""
    closed = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={"title": "T", "summary_text": "x", "closes_at": "2020-01-01T09:00"},
    )
    assert "status-closed" in closed.text
    assert "Votre vote" not in closed.text, "a closed votation shows results, not a ballot"

    open_ = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={"title": "T", "summary_text": "x", "closes_at": "2099-01-01T09:00"},
    )
    assert "status-open" in open_.text
    assert "Votre vote" in open_.text


def test_the_preview_requires_the_admin_guard(client, db_session):
    r = client.post("/admin/lois/apercu", data={"title": "T"})
    assert r.status_code == 401


def test_a_draft_is_linked_to_its_preview_not_to_a_404(client, db_session):
    """Drafts in the dashboard link to their preview (the public URL is a 404)."""
    draft = make_law(db_session, slug="loi-brouillon", title="Loi en brouillon", published=False)

    dashboard = client.get("/admin", headers=admin_headers())
    assert f'href="/admin/lois/{draft.id}/apercu"' in dashboard.text
    assert f'href="/loi/{draft.slug}"' not in dashboard.text
    assert client.get(f"/loi/{draft.slug}").status_code == 404, "still private, as intended"
    assert client.get(f"/admin/lois/{draft.id}/apercu", headers=admin_headers()).status_code == 200


def test_a_published_law_still_links_to_its_public_page(client, db_session):
    law = make_law(db_session, slug="loi-publiee", title="Loi publiée")
    dashboard = client.get("/admin", headers=admin_headers())
    assert f'href="/loi/{law.slug}"' in dashboard.text
    assert f"/loi/{law.slug}/export.csv" in dashboard.text


# --- Triaging the import queue ------------------------------------------------


def _staged(db_session, uid, *, procedure, seances=0, promulgated=None):
    from datetime import date

    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid=uid,
        title=f"Loi {uid}",
        law_code=uid[-4:],
        procedure=procedure,
        promulgated_on=promulgated or date(2026, 1, 1),
        seances=[{"reunion_ref": f"r{i}", "odj_ref": "o", "date": ""} for i in range(seances)],
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_the_queue_can_be_narrowed_to_one_procedure(client, db_session):
    """The import queue can be filtered by procedure."""
    _staged(db_session, "DLR17N001", procedure="Projet de ratification des traités")
    _staged(db_session, "DLR17N002", procedure="Projet de ratification des traités")
    _staged(db_session, "DLR17N003", procedure="Proposition de loi ordinaire")

    page = client.get(
        "/admin/imports?procedure=Projet+de+ratification+des+trait%C3%A9s",
        headers=admin_headers(),
    ).text

    assert "Loi DLR17N001" in page
    assert "Loi DLR17N002" in page
    assert "Loi DLR17N003" not in page, "the filter let another procedure through"


def test_the_procedure_choices_carry_their_counts(client, db_session):
    """Each procedure option shows its count."""
    for i in range(3):
        _staged(db_session, f"DLR17N10{i}", procedure="Projet de ratification des traités")
    _staged(db_session, "DLR17N200", procedure="Proposition de loi ordinaire")

    page = client.get("/admin/imports", headers=admin_headers()).text
    assert "Projet de ratification des traités (3)" in page
    assert "Proposition de loi ordinaire (1)" in page


def test_the_queue_can_be_ranked_by_how_much_a_text_was_argued_over(client, db_session):
    """The queue can be sorted by number of sittings."""
    _staged(db_session, "DLR17N301", procedure="Proposition de loi ordinaire", seances=1)
    _staged(db_session, "DLR17N302", procedure="Proposition de loi ordinaire", seances=12)
    _staged(db_session, "DLR17N303", procedure="Proposition de loi ordinaire", seances=4)

    page = client.get("/admin/imports?tri=seances", headers=admin_headers()).text
    order = [page.index(f"Loi DLR17N30{n}") for n in (2, 3, 1)]
    assert order == sorted(order), "the most debated text must come first"


def test_the_sitting_count_is_shown_per_row(client, db_session):
    _staged(db_session, "DLR17N400", procedure="Proposition de loi ordinaire", seances=7)
    page = client.get("/admin/imports", headers=admin_headers()).text
    assert '<td class="seance-count">7</td>' in page


def test_an_unknown_sort_falls_back_rather_than_failing(client, db_session):
    _staged(db_session, "DLR17N500", procedure="Proposition de loi ordinaire", seances=2)
    r = client.get("/admin/imports?tri=n_importe_quoi", headers=admin_headers())
    assert r.status_code == 200


def test_the_filters_survive_turning_the_page(client, db_session):
    """Filters are kept across pages."""
    for i in range(3):
        _staged(db_session, f"DLR17N6{i:02}", procedure="Proposition de loi ordinaire", seances=i)

    page = client.get(
        "/admin/imports?procedure=Proposition+de+loi+ordinaire&tri=seances",
        headers=admin_headers(),
    ).text
    if "pagination" in page:
        assert "procedure=Proposition" in page.split("pagination")[1]
    # The controls show the current choice.
    assert 'value="Proposition de loi ordinaire" selected' in page
    assert 'value="seances" selected' in page


# --- Promulgation date ------------------------------------------------------------


def test_the_promulgation_date_is_saved_and_shown(client, db_session):
    """The promulgation date can be set in the form."""
    law = make_law(db_session, slug="loi-promulguee", promulgated_on=None)
    client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": law.title, "promulgated_on": "2026-06-24", "published": "1"},
        follow_redirects=False,
    )
    db_session.refresh(law)
    assert law.promulgated_on == date(2026, 6, 24)

    form = client.get(f"/admin/lois/{law.id}", headers=admin_headers()).text
    assert 'name="promulgated_on"' in form and 'value="2026-06-24"' in form


def test_changing_the_promulgation_date_is_recorded_publicly(client, db_session):
    """Changing the promulgation date is recorded in the public history."""
    law = make_law(db_session, slug="loi-redatee", promulgated_on=date(2026, 1, 5))
    client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": law.title, "promulgated_on": "2026-03-09", "published": "1"},
        follow_redirects=False,
    )
    entry = db_session.query(LawRevision).filter_by(law_id=law.id, field="promulgated_on").one()
    # Shown as a French date, not ISO.
    assert entry.old_value == "5 janvier 2026"
    assert entry.new_value == "9 mars 2026"
    assert "Date de promulgation" in client.get(f"/loi/{law.slug}/resultats").text


def test_an_unreadable_promulgation_date_is_refused_with_a_message(client, db_session):
    law = make_law(db_session, slug="loi-date-illisible")
    r = client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": law.title, "promulgated_on": "le 24 juin", "published": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "illisible" in r.text


def test_previewing_an_unsaved_law_touches_no_memo_and_no_query(client, db_session):
    """Previewing an unsaved law (id None) runs no tally query and writes no memo."""
    from app.crud import results as results_crud

    results_crud.reset_cache()
    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={
            "title": "Loi jamais enregistrée",
            "summary_text": "Un résumé.",
            # Closed, so the page shows a tally.
            "opens_at": "2020-01-01T10:00",
            "closes_at": "2020-02-01T10:00",
        },
    )
    assert r.status_code == 200
    assert "Loi jamais enregistrée" in r.text
    assert results_crud._TALLIES.get(None, max_age=3600) is None  # type: ignore[arg-type]
    assert results_crud._TALLIES._entries == {}, "a preview left an entry behind"


def test_a_double_click_on_create_makes_one_votation(sessionmaker_for):
    """Two simultaneous creations from one dossier: one law, and a 409 for the other.

    Both used to read the dossier as unlinked and each create a law.
    """
    Session = sessionmaker_for
    setup = Session()
    now = datetime.now(UTC)
    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DDOUBLE",
        title="Loi cliquée deux fois",
        law_code="2026-10",
        promulgated_on=date(2026, 1, 1),
        dossier_url="https://an.fr",
        first_seen_at=now,
        last_seen_at=now,
    )
    setup.add(item)
    setup.commit()
    item_id = item.id
    form = LawForm(
        title="Loi cliquée deux fois",
        official_ref="",
        legifrance_url="",
        summary_text="",
        practical_changes="",
        arguments_for=[],
        arguments_against=[],
        debate_summary="",
        promulgated_on=None,
        opens_at=None,
        closes_at=None,
        published=False,
    )
    start = threading.Barrier(2, timeout=10)
    outcomes: list[str] = []

    def submit() -> None:
        db = Session()
        try:
            start.wait()
            create_law(db=db, form=form, import_id=item_id)
            outcomes.append("created")
        except HTTPException as exc:
            outcomes.append(str(exc.status_code))
        except Exception as exc:  # shown by the assert below
            outcomes.append(repr(exc))
        finally:
            db.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    try:
        assert sorted(outcomes) == ["409", "created"], outcomes
        assert setup.query(Law).filter_by(title=form.title).count() == 1
    finally:
        # The dossier first: it references the law.
        setup.execute(delete(ImportedLaw).where(ImportedLaw.id == item_id))
        setup.execute(delete(Law).where(Law.title == form.title))
        setup.commit()
        setup.close()


# --- A closed vote that has votes keeps its closing date ------------------------


def _save_form(client, law, **fields):
    """Post the admin form with the law's current values, overridden by ``fields``."""
    data = {
        "title": law.title,
        "published": "1",
        "opens_at": dt_local(law.opens_at),
        "closes_at": dt_local(law.closes_at),
        **fields,
    }
    return client.post(
        f"/admin/lois/{law.id}", headers=admin_headers(), data=data, follow_redirects=False
    )


def test_a_closed_vote_with_votes_cannot_be_reopened(client, closed_law, db_session):
    """Reopening would let the next snapshot step be compared with the closing page."""
    db_session.add(Vote(law_id=closed_law.id, voter_token="avant-cloture", choice="for"))
    db_session.commit()
    closing = closed_law.closes_at

    later = dt_local(datetime.now(UTC) + timedelta(days=3))
    r = _save_form(client, closed_law, closes_at=later)
    assert r.status_code == 400
    assert "ne peut plus changer" in r.text
    db_session.refresh(closed_law)
    assert closed_law.closes_at == closing

    r = _save_form(client, closed_law, closes_at="")
    assert r.status_code == 400, "clearing the date would reopen it too"

    r = _save_form(client, closed_law, title="Loi close, titre corrigé")
    assert r.status_code == 303, "other edits are still fine"
    db_session.refresh(closed_law)
    assert closed_law.title == "Loi close, titre corrigé"


def test_a_closed_vote_without_votes_can_be_reopened(client, closed_law, db_session):
    later = datetime.now(UTC) + timedelta(days=3)
    r = _save_form(client, closed_law, closes_at=dt_local(later))
    assert r.status_code == 303
    db_session.refresh(closed_law)
    assert closed_law.is_open()


def test_saving_a_closed_vote_keeps_its_stored_times(client, db_session):
    """The form drops seconds and reads the repeated autumn hour as its first one.

    Saving another field must neither refuse the closed vote nor move or log
    its times.
    """
    # 01:30 UTC on 26 October 2025 is 02:30 in Paris for the second time (CET).
    law = make_law(
        db_session,
        slug="loi-heure-repetee",
        opens_at=datetime(2025, 10, 1, 8, 0, 17, tzinfo=UTC),
        closes_at=datetime(2025, 10, 26, 1, 30, tzinfo=UTC),
    )
    db_session.add(Vote(law_id=law.id, voter_token="avant-cloture", choice="for"))
    db_session.commit()

    r = _save_form(client, law, title="Loi à l'heure répétée")
    assert r.status_code == 303
    db_session.refresh(law)
    assert law.title == "Loi à l'heure répétée"
    assert law.closes_at == datetime(2025, 10, 26, 1, 30, tzinfo=UTC)
    assert law.opens_at == datetime(2025, 10, 1, 8, 0, 17, tzinfo=UTC)
    fields = {r.field for r in db_session.query(LawRevision).filter_by(law_id=law.id)}
    assert fields == {"title"}, "only the real change is in the public history"
