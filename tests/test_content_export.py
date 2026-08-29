"""The editorial content survives a move to another database.

The export is only worth having if it restores, so every test here is a round
trip rather than an assertion about the file's shape. Ids deliberately do not
travel: the second database allocates its own, and everything is keyed on the
slug and the dossier uid instead.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.crud.laws import TRACKED_FIELDS
from app.models import ImportedLaw, Law, LawRevision, Vote
from scripts.content import (
    _NOT_CARRIED,
    LAW_FIELDS,
    ContentFormatError,
    ExportDirectoryError,
    PublishedLawError,
    build_document,
    export,
    import_content,
)
from tests.helpers import admin_headers, make_law


def _stage(db, *, uid: str, status: str = "nouveau", law_id: int | None = None) -> ImportedLaw:
    item = ImportedLaw(
        dossier_uid=uid,
        title="Dossier",
        law_code="2026-1",
        promulgated_on=datetime.now(UTC).date(),
        status=status,
        law_id=law_id,
    )
    db.add(item)
    db.commit()
    return item


def _wipe(db) -> None:
    """Everything the export is supposed to be able to put back."""
    db.query(LawRevision).delete()
    db.query(Vote).delete()
    db.query(ImportedLaw).delete()
    db.query(Law).delete()
    db.commit()


# --- The laws themselves ------------------------------------------------------


def test_a_law_comes_back_word_for_word(db_session):
    make_law(
        db_session,
        slug="loi-complete",
        title="Loi complète",
        official_ref="LOI n° 2026-1 du 1 janvier 2026",
        legifrance_url="https://legifrance.gouv.fr/x",
        summary_text="Premier paragraphe.\n\nSecond paragraphe.",
        practical_changes="Ce qui change.",
        arguments_for=[{"text": "Pour", "author": "A", "source": "https://an.fr/1"}],
        arguments_against=[{"text": "Contre", "author": "B", "source": "https://an.fr/2"}],
    )
    document = build_document(db_session)
    _wipe(db_session)

    import_content_from(db_session, document)

    restored = db_session.query(Law).filter_by(slug="loi-complete").one()
    assert restored.title == "Loi complète"
    assert restored.official_ref == "LOI n° 2026-1 du 1 janvier 2026"
    assert restored.summary_text == "Premier paragraphe.\n\nSecond paragraphe."
    assert restored.arguments_for[0]["author"] == "A"
    assert restored.arguments_against[0]["source"] == "https://an.fr/2"


def test_the_voting_window_keeps_its_instants(db_session):
    opens = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
    closes = datetime(2027, 3, 31, 18, 30, tzinfo=UTC)
    make_law(db_session, slug="loi-fenetre", opens_at=opens, closes_at=closes)
    document = build_document(db_session)
    _wipe(db_session)
    import_content_from(db_session, document)

    restored = db_session.query(Law).filter_by(slug="loi-fenetre").one()
    assert restored.opens_at == opens
    assert restored.closes_at == closes


def test_the_promulgation_date_travels(db_session):
    """It is what the home page is ordered by, so losing it reorders the site.

    The column was added, the form learned to write it and the listings learned
    to sort on it — and the snapshot was not told, so a move to another host put
    every law's date back as empty and the new home page fell back to the order
    the rows happened to be inserted in.
    """
    make_law(db_session, slug="loi-datee", promulgated_on=date(2026, 6, 24))
    document = build_document(db_session)
    _wipe(db_session)
    import_content_from(db_session, document)

    restored = db_session.query(Law).filter_by(slug="loi-datee").one()
    assert restored.promulgated_on == date(2026, 6, 24), "a day, not an instant"


def test_every_column_of_a_law_is_carried_or_deliberately_left_out(db_session):
    """The guard the missing promulgation date needed and did not have.

    An omission here is invisible: the export runs, the import runs, and the
    column simply comes back empty. So every column has to be *named* — either as
    something that travels or as something explicitly left behind — and adding
    one to the model without deciding which fails this test rather than a
    restore, six months later, on another host.
    """
    columns = {column.name for column in Law.__table__.c}
    undecided = columns - set(LAW_FIELDS) - _NOT_CARRIED
    assert not undecided, (
        f"{sorted(undecided)} : ajoutez ces colonnes à LAW_FIELDS, ou à _NOT_CARRIED "
        "avec la raison de ne pas les emporter"
    )
    # And nothing names a column that no longer exists.
    assert set(LAW_FIELDS) <= columns
    # Everything whose change is published must survive the move: the results page
    # states what moved since publication, and it cannot do that about a value the
    # snapshot dropped.
    assert set(TRACKED_FIELDS) <= set(LAW_FIELDS)


def test_a_slug_from_the_file_is_re_derived_not_trusted(db_session):
    """This file is the one input that arrives as a whole document from outside.

    A slug is a URL segment and reaches a `Content-Disposition` header on the
    export routes, so it goes through `slugify` on the way in like every slug the
    application makes itself — rather than being taken on faith from a JSON file.
    """
    make_law(db_session, slug="loi-propre")
    document = build_document(db_session)
    _wipe(db_session)
    document["laws"][0]["slug"] = "Loi Sale\r\nX-Injected: 1"

    import_content_from(db_session, document)

    restored = db_session.query(Law).one()
    assert restored.slug == "loi-sale-x-injected-1"
    assert "\r" not in restored.slug and "\n" not in restored.slug


def test_drafts_and_archived_laws_travel_too(db_session):
    """A presentation in progress is exactly what costs a day to rewrite."""
    make_law(db_session, slug="loi-brouillon", title="Brouillon", published=False)
    make_law(
        db_session,
        slug="loi-archivee",
        title="Archivée",
        archived_at=datetime.now(UTC) - timedelta(days=2),
    )
    document = build_document(db_session)
    _wipe(db_session)
    import_content_from(db_session, document)

    assert db_session.query(Law).filter_by(slug="loi-brouillon").one().published is False
    assert db_session.query(Law).filter_by(slug="loi-archivee").one().is_archived


# --- The public record of changes ---------------------------------------------


def test_the_history_of_changes_travels_with_the_law(client, db_session):
    """Without it the new site would claim a modified text was never touched."""
    law = make_law(db_session, slug="loi-modifiee", title="Loi modifiée", published=True)
    for version in ("première version", "seconde version"):
        client.post(
            f"/admin/lois/{law.id}",
            headers=admin_headers(),
            data={"title": "Loi modifiée", "summary_text": version, "published": "1"},
            follow_redirects=False,
        )
    before = db_session.query(LawRevision).filter_by(law_id=law.id).count()
    assert before, "the edits really were recorded"

    document = build_document(db_session)
    _wipe(db_session)
    import_content_from(db_session, document)

    restored = db_session.query(Law).filter_by(slug="loi-modifiee").one()
    after = db_session.query(LawRevision).filter_by(law_id=restored.id).all()
    assert len(after) == before
    assert {r.field for r in after} == {"summary_text", "opens_at", "closes_at"}
    # And the page that reads them says so rather than claiming nothing moved.
    page = client.get("/loi/loi-modifiee/resultats").text
    assert "Aucune modification depuis la publication" not in page


# --- The review decisions -----------------------------------------------------


def test_an_ignored_import_stays_ignored(db_session):
    """The daily job rebuilds the facts but never touches the decision."""
    _stage(db_session, uid="DLR5L17N1", status="ignore")
    document = build_document(db_session)

    # A fresh instance: the import job has re-seen the dossier, as "nouveau".
    _wipe(db_session)
    _stage(db_session, uid="DLR5L17N1", status="nouveau")
    import_content_from(db_session, document)

    assert db_session.query(ImportedLaw).filter_by(dossier_uid="DLR5L17N1").one().status == "ignore"


def test_a_decision_is_relinked_to_the_law_by_slug_not_by_id(db_session):
    """Ids are allocated afresh on the other side; the slug is what carries."""
    law = make_law(db_session, slug="loi-issue-d-un-import")
    _stage(db_session, uid="DLR5L17N2", status="importe", law_id=law.id)
    document = build_document(db_session)

    _wipe(db_session)
    # Burn some ids so the law cannot land on the same one by luck.
    for n in range(3):
        make_law(db_session, slug=f"remplissage-{n}")
    _stage(db_session, uid="DLR5L17N2", status="nouveau")
    import_content_from(db_session, document)

    item = db_session.query(ImportedLaw).filter_by(dossier_uid="DLR5L17N2").one()
    restored = db_session.query(Law).filter_by(slug="loi-issue-d-un-import").one()
    assert item.status == "importe"
    assert item.law_id == restored.id
    assert item.law_id != law.id, "the test would prove nothing if the id repeated"


def test_a_decision_for_a_dossier_not_yet_imported_is_reported_not_invented(db_session):
    """The staged row's factual columns come from the AN export, never from here."""
    _stage(db_session, uid="DLR5L17N3", status="ignore")
    document = build_document(db_session)
    _wipe(db_session)

    counts = import_content_from(db_session, document)
    assert counts["decisions"] == 0
    assert counts["decisions_pending"] == 1
    assert db_session.query(ImportedLaw).count() == 0


# --- What is deliberately absent ----------------------------------------------


def test_the_export_carries_no_vote_and_no_token(client, db_session, open_law):
    """The votes are the dump's job; a reduced copy here would be mistaken for one."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    blob = json.dumps(build_document(db_session), ensure_ascii=False)
    assert "voter_token" not in blob
    assert "demographic" not in blob.lower()
    assert '"votes"' not in blob


# --- Running it twice ---------------------------------------------------------


def test_importing_twice_changes_nothing_the_second_time(db_session):
    make_law(db_session, slug="loi-idempotente", title="Idempotente")
    document = build_document(db_session)
    _wipe(db_session)

    first = import_content_from(db_session, document)
    second = import_content_from(db_session, document)
    assert first["created"] == 1
    assert second == {**second, "created": 0, "skipped": 1}
    assert db_session.query(Law).filter_by(slug="loi-idempotente").count() == 1


# --- The file on disk ---------------------------------------------------------


def test_export_writes_a_readable_file_and_prunes_old_ones(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.content.SessionLocal", lambda: _NoClose(db_session))
    make_law(db_session, slug="loi-fichier", title="Loi fichier")

    stale = tmp_path / "votations-contenu-20200101-000000.json"
    stale.write_text("{}", encoding="utf-8")
    import os

    old = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    os.utime(stale, (old, old))

    target = export(tmp_path, keep_days=5)
    assert target.exists()
    assert not stale.exists(), "a snapshot past the retention window is dropped"
    assert not list(tmp_path.glob("*.partial")), "no half-written file is left behind"

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["format"] == 1
    assert [law["slug"] for law in document["laws"]] == ["loi-fichier"]


def test_the_wreckage_of_a_killed_export_is_collected(db_session, tmp_path, monkeypatch):
    """A `.json.partial` is not a snapshot, so the retention sweep never saw one.

    An export killed mid-write therefore left a full-size file in the bind mount
    for ever, on the same disk as PostgreSQL — the failure `backup.sh` already
    guards against with `clean_partials`.
    """
    import os

    monkeypatch.setattr("scripts.content.SessionLocal", lambda: _NoClose(db_session))
    make_law(db_session, slug="loi-fichier", title="Loi fichier")

    wreck = tmp_path / "votations-contenu-20200101-000000.json.partial"
    wreck.write_text("{tronqu", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(wreck, (old, old))

    # A run that started a moment ago is somebody else's work in progress.
    fresh = tmp_path / "votations-contenu-20990101-000000.json.partial"
    fresh.write_text("{en cours", encoding="utf-8")

    export(tmp_path, keep_days=5)

    assert not wreck.exists()
    assert fresh.exists(), "a partial being written right now must not be swept"


def test_a_file_from_another_format_is_refused(tmp_path):
    path = tmp_path / "vieux.json"
    path.write_text(json.dumps({"format": 99, "laws": []}), encoding="utf-8")
    with pytest.raises(ContentFormatError, match="format 99"):
        import_content(path)


def test_something_that_is_not_an_export_at_all_is_refused(tmp_path):
    path = tmp_path / "autre.json"
    path.write_text(json.dumps({"quelque": "chose"}), encoding="utf-8")
    with pytest.raises(ContentFormatError, match="pas un export"):
        import_content(path)


# --- Plumbing -----------------------------------------------------------------


class _NoClose:
    """Hands the test's own session to code that opens and closes its own."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


def import_content_from(db, document: dict, *, replace: bool = False) -> dict[str, int]:
    """Import an in-memory document through the real code path, via a temp file."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False)
        path = Path(handle.name)
    try:
        with patch("scripts.content.SessionLocal", lambda: _NoClose(db)):
            return import_content(path, replace=replace)
    finally:
        path.unlink()


# --- Replacing is guarded on a published law ----------------------------------


def test_replacing_a_published_law_is_refused(db_session):
    """`--replace` writes straight to the columns and records no revision.

    On a published law that means rewriting the text people voted on while the
    public history keeps saying nothing moved — the transparency claim made false
    in one command. Editing a published law goes through /admin, where it is
    recorded.
    """
    make_law(db_session, slug="loi-en-cours", title="Titre publié", published=True)
    document = build_document(db_session)
    law = db_session.query(Law).filter_by(slug="loi-en-cours").one()
    law.title = "Titre modifié depuis"
    db_session.commit()

    with pytest.raises(PublishedLawError, match="loi-en-cours"):
        import_content_from(db_session, document, replace=True)
    db_session.rollback()
    assert db_session.query(Law).filter_by(slug="loi-en-cours").one().title == (
        "Titre modifié depuis"
    ), "nothing was written before the refusal"


def test_replacing_an_unpublished_draft_is_fine(db_session):
    """Nobody has voted on a draft, so there is no record to falsify."""
    make_law(db_session, slug="loi-brouillon-2", title="Titre d'origine", published=False)
    document = build_document(db_session)
    law = db_session.query(Law).filter_by(slug="loi-brouillon-2").one()
    law.title = "Titre modifié depuis"
    db_session.commit()

    import_content_from(db_session, document, replace=True)
    assert db_session.query(Law).filter_by(slug="loi-brouillon-2").one().title == "Titre d'origine"


def test_a_published_law_is_still_skipped_without_replace(db_session):
    """The default path is untouched by the guard: it never reaches it."""
    make_law(db_session, slug="loi-publiee-3", title="Titre publié", published=True)
    document = build_document(db_session)
    counts = import_content_from(db_session, document)
    assert counts["skipped"] == 1


# --- A directory the container cannot write to --------------------------------


def test_an_unwritable_export_directory_names_the_fix(db_session, tmp_path, monkeypatch):
    """The predictable failure of a bind mount deserves better than a traceback."""
    monkeypatch.setattr("scripts.content.SessionLocal", lambda: _NoClose(db_session))
    locked = tmp_path / "verrouille"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ExportDirectoryError, match="EXPORT_UID"):
            export(locked, keep_days=5)
    finally:
        locked.chmod(0o700)
