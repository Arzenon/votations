"""The AI draft banner: a small state machine that must not lose its message."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.helpers import admin_headers

# --- D1: the draft banner stops nagging, but only after it has been seen -------


def test_a_failed_draft_is_shown_until_it_is_dismissed(client, db_session):
    """A failure must be seen once, then stop nagging — without a GET clearing it.

    Clearing it while *rendering* the form meant an ordinary GET destroyed the
    only record of why the draft had not happened, so a link prefetch or a
    browser preload could consume the explanation before anybody read it.
    """
    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ECHEC",
        title="Dossier",
        law_code="2026-9",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    imports_crud.fail_draft(db_session, item, message="le texte officiel est introuvable")

    page = f"/admin/lois/nouvelle?import_id={item.id}"
    assert "introuvable" in client.get(page, headers=admin_headers()).text, (
        "the administrator must see why it failed"
    )
    assert "introuvable" in client.get(page, headers=admin_headers()).text, (
        "and a second GET must not have eaten the message"
    )

    dismissed = client.post(
        f"/admin/imports/{item.id}/brouillon/masquer",
        headers=admin_headers(),
        follow_redirects=False,
    )
    assert dismissed.status_code == 303
    assert "introuvable" not in client.get(page, headers=admin_headers()).text, (
        "once acknowledged, it must not be told again for ever"
    )


def test_dismissing_a_draft_failure_needs_the_admin_guard(client, db_session):
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ECHEC-AUTH",
        title="Dossier",
        law_code="2026-11",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    r = client.post(f"/admin/imports/{item.id}/brouillon/masquer", follow_redirects=False)
    assert r.status_code == 401


def test_a_pending_draft_is_never_cleared_by_opening_the_form(client, db_session):
    """Clearing it mid-run would stop the polling banner while the job continues."""
    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ENCOURS",
        title="Dossier",
        law_code="2026-10",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    imports_crud.mark_draft_pending(db_session, item)

    page = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=admin_headers())
    assert "en cours de génération" in page.text
    db_session.refresh(item)
    assert item.draft_status == "en_cours"
