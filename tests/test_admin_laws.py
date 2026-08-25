"""Administering a law: the public record of edits, archiving, purging, preview."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud.laws import REVISIONS_PAGE_SIZE
from app.models import Law, LawRevision
from tests.helpers import admin_headers, make_law

# --- B2: the public record of edits made after publication -------------------


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


# --- B3: archiving replaces deletion -----------------------------------------


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
    """The confirmation used to be a JS attribute; with JS off it did nothing."""
    client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    page = client.get(f"/admin/lois/{open_law.id}/supprimer", headers=admin_headers())
    assert page.status_code == 200
    assert "<form" in page.text
    assert "irréversible" in page.text


def test_a_law_cannot_be_purged_before_it_is_archived(client, db_session, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer", headers=admin_headers(), follow_redirects=False
    )
    assert r.status_code == 409
    assert db_session.get(Law, open_law.id) is not None


def test_purging_an_archived_law_removes_it_for_good(client, db_session, open_law):
    law_id = open_law.id
    client.post(
        f"/admin/lois/{law_id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    r = client.post(
        f"/admin/lois/{law_id}/supprimer", headers=admin_headers(), follow_redirects=False
    )
    assert r.status_code == 303
    assert db_session.get(Law, law_id) is None


# --- Aperçu d'une loi non publiée --------------------------------------------


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
    assert "Votre vote" in page, "le bulletin doit être visible tel qu'il sera"
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
# The import service retries forever on failure, so a source that moves — and the
# export URL carries the legislature number, so it does move — would stop the
# imports in silence. The dashboard is where that silence becomes visible.


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
    # It names the service to look at, rather than leaving the reader guessing.
    assert "assemblee-refresh" in page


def test_never_having_imported_counts_as_stale(client, db_session):
    """An import that has never once succeeded is the loudest failure of all."""
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
    """Every edit adds a row holding the whole before *and* after value.

    Unbounded, on a public page: a law corrected a few hundred times shipped all
    of it in one table to every reader of its results.

    The count is read back rather than assumed: saving the form also blanks the
    dates it does not carry, so the first edit records more than one change.
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
