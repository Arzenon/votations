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


# --- Shutting down must not wait for a draft ---------------------------------


def test_a_running_draft_never_holds_the_process_open():
    """The whole of CORR-1, and the reason the pool is not a ThreadPoolExecutor.

    Its workers are ordinary threads, and `concurrent.futures.thread` joins every
    one of them at interpreter exit — so `shutdown(wait=False,
    cancel_futures=True)` returned instantly and the process then sat there for
    as long as the draft had left. Docker gives up after ten seconds, which made
    a redeploy during a draft end in SIGKILL.
    """
    import threading

    from app.drafting import _DraftPool

    started = threading.Event()
    release = threading.Event()

    def slow_draft():
        started.set()
        release.wait(timeout=30)

    pool = _DraftPool(workers=1)
    pool.submit(slow_draft)
    assert started.wait(timeout=5), "the job should be running"

    worker = next(t for t in threading.enumerate() if t.name == "draft" and t.is_alive())
    assert worker.daemon is True, "a non-daemon worker is joined at exit - CORR-1 is back"
    release.set()


def test_a_queued_draft_is_not_started_once_the_service_is_stopping(monkeypatch):
    """`cancel_futures` used to be what dropped these; now the flag is."""
    from app import drafting

    monkeypatch.setattr(drafting, "_stopping", drafting.threading.Event())
    drafting._stopping.set()

    ran = []
    pool = drafting._DraftPool(workers=1)
    pool.submit(ran.append, "should not run")
    drafting.time.sleep(0.2)
    assert ran == []


def test_a_draft_between_steps_stops_with_a_readable_reason(monkeypatch):
    """A cooperative stop still records *why*, rather than vanishing."""
    import pytest

    from app import drafting
    from app.opendata import DeadlineExceeded

    monkeypatch.setattr(drafting, "_stopping", drafting.threading.Event())
    deadline = drafting._Deadline.start(600)
    deadline.check("texte officiel")  # healthy: nothing raised

    drafting._stopping.set()
    with pytest.raises(DeadlineExceeded) as caught:
        deadline.check("texte officiel")
    assert "s'est arrêté" in str(caught.value)
    assert "texte officiel" in str(caught.value)
