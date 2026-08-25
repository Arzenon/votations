"""The law form: what it accepts, what it refuses, and in whose timezone.

Opening and closing times are typed as Paris civil time and stored as UTC, which
is the single richest source of off-by-an-hour bugs in the application.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.models import Law
from tests.helpers import admin_headers, make_law

PARIS = ZoneInfo("Europe/Paris")


# --- Scheduling in local time -------------------------------------------------


def test_a_closing_time_means_the_hour_the_admin_typed(client, db_session):
    """The form says 20:00; it used to close at 22:00 Paris."""
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi horaire", "closes_at": "2026-07-01T20:00", "published": "1"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi horaire").one()
    assert law.closes_at.astimezone(PARIS).hour == 20
    # July is CEST (UTC+2), so the stored instant is 18:00 UTC.
    assert law.closes_at.astimezone(UTC).hour == 18


def test_winter_and_summer_are_both_right(client, db_session):
    """A fixed offset would be wrong for half the year."""
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi hiver", "closes_at": "2026-01-15T20:00", "published": "1"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi hiver").one()
    # January is CET (UTC+1).
    assert law.closes_at.astimezone(UTC).hour == 19
    assert law.closes_at.astimezone(PARIS).hour == 20


def test_the_edit_form_shows_back_what_was_typed(client, db_session):
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Aller-retour", "closes_at": "2026-07-01T20:00"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Aller-retour").one()
    form = client.get(f"/admin/lois/{law.id}", headers=admin_headers())
    assert 'value="2026-07-01T20:00"' in form.text
    assert "heure de Paris" in form.text


def test_a_law_opens_at_the_local_hour_it_was_scheduled_for(db_session):
    """The status the public sees follows the same clock as the form."""
    opens = datetime(2026, 7, 1, 20, 0, tzinfo=PARIS)
    law = Law(slug="prog", title="Programmée", published=True, opens_at=opens)
    db_session.add(law)
    db_session.commit()
    assert law.status(now=datetime(2026, 7, 1, 19, 59, tzinfo=PARIS)) == "scheduled"
    assert law.status(now=datetime(2026, 7, 1, 20, 1, tzinfo=PARIS)) == "open"


# --- Malformed input ----------------------------------------------------------


def test_an_unreadable_date_is_a_message_not_a_crash(client, db_session):
    r = client.post(
        "/admin/lois",
        headers={**admin_headers(), "Accept": "text/html"},
        data={"title": "Loi", "opens_at": "pas-une-date"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Saisie invalide" in r.text
    assert "pas-une-date" in r.text


def test_a_closing_time_before_the_opening_is_refused(client):
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi impossible",
            "opens_at": "2026-09-01T10:00",
            "closes_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "postérieure" in r.text


def test_a_nonsense_import_id_is_rejected_not_fatal(client, db_session):
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi", "import_id": "abc"},
        follow_redirects=False,
    )
    assert r.status_code == 422


# --- B1 / B4 / B5: the form refuses instead of failing quietly ---------------


def test_malformed_argument_json_is_refused_not_silently_wiped(client, db_session):
    law = make_law(db_session, slug="loi-args", arguments_for=[{"text": "gardé"}])
    r = client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={"title": "Loi X", "arguments_for": '[{"text": "cassé",}]'},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "JSON illisible" in r.text
    db_session.refresh(law)
    assert law.arguments_for == [{"text": "gardé"}], "the existing arguments must survive"


def test_an_over_long_title_is_a_message_not_a_500(client):
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "x" * 501},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "limite" in r.text


def test_an_empty_title_is_refused(client):
    r = client.post(
        "/admin/lois", headers=admin_headers(), data={"title": "   "}, follow_redirects=False
    )
    assert r.status_code == 400


# --- The prose blocks are Text columns, so nothing bounded them ---------------


def test_an_enormous_summary_is_refused_with_a_message(client):
    """`summary_text` had no limit at all: the column is Text and so was the form.

    A whole loi de finances pasted in landed as a multi-megabyte row that the home
    page then read in full to render a 200-character excerpt.
    """
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi", "summary_text": "x" * 20_001},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Ce que dit la loi" in r.text


def test_an_argument_longer_than_a_quotation_is_refused(client):
    payload = json.dumps([{"text": "y" * 5_001, "author": "A", "source": "https://x.example"}])
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi", "arguments_for": payload},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "citation" in r.text


def test_an_ordinary_presentation_still_saves(client, db_session):
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi ordinaire",
            "summary_text": "Un résumé.\n\nUn second paragraphe.",
            "practical_changes": "Ce qui change.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.query(Law).filter_by(title="Loi ordinaire").one()


def test_the_column_limits_are_read_off_the_model_not_copied():
    """Widening a column used to leave the form refusing at the old limit."""
    from app.routes.admin.forms import MAX_LENGTHS

    for name in ("title", "official_ref", "legifrance_url"):
        assert MAX_LENGTHS[name] == Law.__table__.c[name].type.length


# --- The publication checkbox -------------------------------------------------


def test_an_unchecked_box_does_not_publish(client, db_session):
    """A browser omits the field entirely when the box is not ticked."""
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi discrète", "summary_text": "x"},
    )
    law = db_session.query(Law).filter_by(title="Loi discrète").one()
    assert law.published is False


def test_the_word_false_does_not_publish_a_law(client, db_session):
    """`bool("false")` is True, and this field decides whether people can vote.

    No browser sends it — a checkbox is absent or "on" — but this form is also
    posted by replayed requests and by curl, and V3 opens it to every
    authenticated user. Publishing on a value that says the opposite is not a
    failure mode worth keeping for the sake of one character.
    """
    # The title carries the index, not the word: `law_form` strips the title, so
    # a value with spaces around it would not be the title we looked up.
    for index, word in enumerate(("false", "0", "off", "non", " False ")):
        title = f"Loi non publiee {index}"
        client.post(
            "/admin/lois",
            headers=admin_headers(),
            data={"title": title, "summary_text": "x", "published": word},
        )
        law = db_session.query(Law).filter_by(title=title).one()
        assert law.published is False, f"« {word} » published the law"


def test_a_ticked_box_still_publishes(client, db_session):
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi publiée", "summary_text": "x", "published": "on"},
    )
    law = db_session.query(Law).filter_by(title="Loi publiée").one()
    assert law.published is True
