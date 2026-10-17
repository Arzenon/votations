"""The law form: what it accepts and refuses, and time zones.

Times are entered in Paris time and stored in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.models import Law
from app.routes.admin import forms
from tests.helpers import admin_headers, make_law

PARIS = ZoneInfo("Europe/Paris")


# --- Scheduling in local time -------------------------------------------------


def test_a_closing_time_means_the_hour_the_admin_typed(client, db_session):
    """20:00 in the form means 20:00 Paris time."""
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
    """Correct in both winter and summer time."""
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
    """The public status follows the same clock as the form."""
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


# --- Invalid input is refused with a message ------------------------------------


def test_a_half_filled_argument_is_refused_not_silently_wiped(client, db_session):
    """An argument with an author but no quote is refused, not silently dropped."""
    law = make_law(db_session, slug="loi-args", arguments_for=[{"text": "gardé"}])
    r = client.post(
        f"/admin/lois/{law.id}",
        headers=admin_headers(),
        data={
            "title": "Loi X",
            "arguments_for_text": "",
            "arguments_for_author": "Mme X",
            "arguments_for_source": "https://an.fr/cr",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "citation est vide" in r.text
    db_session.refresh(law)
    assert law.arguments_for == [{"text": "gardé"}], "the existing arguments must survive"


def test_an_entirely_empty_argument_row_is_simply_skipped(client, db_session):
    """An empty argument row is skipped."""
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi sans argument",
            "arguments_for_text": "",
            "arguments_for_author": "",
            "arguments_for_source": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    law = db_session.query(Law).filter_by(title="Loi sans argument").one()
    assert law.arguments_for == []


def test_arguments_are_paired_by_position(client, db_session):
    """The three lists are matched by position."""
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        # Repeated fields, as the browser posts them.
        data={
            "title": "Loi à deux arguments",
            "arguments_for_text": [
                "Premier argument, assez long pour être publié.",
                "Second argument, tout aussi défendable.",
            ],
            "arguments_for_author": ["Mme A", "M. B"],
            "arguments_for_source": ["https://an.fr/1", "https://an.fr/2"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    law = db_session.query(Law).filter_by(title="Loi à deux arguments").one()
    assert law.arguments_for == [
        {
            "text": "Premier argument, assez long pour être publié.",
            "author": "Mme A",
            "source": "https://an.fr/1",
        },
        {
            "text": "Second argument, tout aussi défendable.",
            "author": "M. B",
            "source": "https://an.fr/2",
        },
    ]


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


# --- Length limits ------------------------------------------------------------


def test_an_enormous_summary_is_refused_with_a_message(client):
    """An enormous summary is refused with a message."""
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi", "summary_text": "x" * 20_001},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Ce que dit la loi" in r.text


def test_an_argument_longer_than_a_quotation_is_refused(client):
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi",
            "arguments_for_text": "y" * 5_001,
            "arguments_for_author": "A",
            "arguments_for_source": "https://x.example",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "citation" in r.text


def test_more_arguments_than_a_side_may_carry_are_refused(client, db_session):
    """Too many arguments on one side are refused."""
    over = forms.MAX_ARGUMENTS_PER_SIDE + 1
    r = client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi bavarde",
            "arguments_for_text": [f"Citation numéro {i}." for i in range(over)],
            "arguments_for_author": [f"Orateur {i}" for i in range(over)],
            "arguments_for_source": ["https://x.example"] * over,
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert str(over) in r.text and str(forms.MAX_ARGUMENTS_PER_SIDE) in r.text
    assert db_session.query(Law).filter_by(title="Loi bavarde").one_or_none() is None


def test_exactly_the_limit_is_accepted(client, db_session):
    """Exactly the limit is accepted."""
    n = forms.MAX_ARGUMENTS_PER_SIDE
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={
            "title": "Loi à la limite",
            "arguments_for_text": [f"Citation numéro {i}." for i in range(n)],
            "arguments_for_author": [f"Orateur {i}" for i in range(n)],
            "arguments_for_source": ["https://x.example"] * n,
        },
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi à la limite").one()
    assert len(law.arguments_for) == n


def test_the_preview_truncates_the_arguments_rather_than_dropping_them(client):
    """The preview truncates the argument list rather than emptying it."""
    over = forms.MAX_ARGUMENTS_PER_SIDE + 3
    r = client.post(
        "/admin/lois/apercu",
        headers=admin_headers(),
        data={
            "title": "Loi bavarde",
            "arguments_for_text": [f"Citation numéro {i}." for i in range(over)],
            "arguments_for_author": [f"Orateur {i}" for i in range(over)],
            "arguments_for_source": ["https://x.example"] * over,
        },
    )
    assert r.status_code == 200
    assert "Citation numéro 0." in r.text, "the preview kept what it could show"
    assert f"Citation numéro {over - 1}." not in r.text, "and cut the rest"


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
    """Length limits come from the model."""
    from app.routes.admin.forms import MAX_LENGTHS

    for name in ("title", "official_ref", "legifrance_url"):
        assert MAX_LENGTHS[name] == Law.__table__.c[name].type.length


# --- The publication checkbox -------------------------------------------------


def test_an_unchecked_box_does_not_publish(client, db_session):
    """An unchecked box (field absent) doesn't publish."""
    client.post(
        "/admin/lois",
        headers=admin_headers(),
        data={"title": "Loi discrète", "summary_text": "x"},
    )
    law = db_session.query(Law).filter_by(title="Loi discrète").one()
    assert law.published is False


def test_the_word_false_does_not_publish_a_law(client, db_session):
    """`published=false` doesn't publish (bool("false") is True)."""
    # The title holds the index, since law_form strips it.
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
