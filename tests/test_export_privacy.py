"""Export must contain NO identifying information."""

import csv
import io
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.models import Vote


def test_csv_export_has_no_pii(client, open_law, db_session):
    db_session.add(Vote(law_id=open_law.id, voter_token="secret-token", choice="for"))
    db_session.commit()

    r = client.get(f"/loi/{open_law.slug}/export.csv")
    assert r.status_code == 200
    assert "secret-token" not in r.text

    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0].keys() == {"law_id", "choice", "created_at"}.intersection(rows[0].keys())
    assert set(rows[0].keys()) == {"law_id", "choice", "created_at"}
    assert "voter_token" not in r.text


def test_json_export_has_no_voter_token(client, open_law, db_session):
    db_session.add(Vote(law_id=open_law.id, voter_token="secret-token", choice="abstain"))
    db_session.commit()

    data = client.get(f"/loi/{open_law.slug}/export.json").json()
    assert "secret-token" not in client.get(f"/loi/{open_law.slug}/export.json").text
    for vote in data["votes"]:
        assert set(vote.keys()) == {"law_id", "choice", "created_at"}


# --- Timestamp resolution -----------------------------------------------------
#
# The evolution curve is withheld below MIN_BUCKET_VOLUME to prevent
# re-identification. Publishing the exact instant of every vote would hand all of
# that back: the curve can be rebuilt at any resolution, and anyone who knows when
# a person voted reads their choice straight off the file.


def _seed(db_session, law, n=60):
    """Seed n votes. The default clears MIN_BUCKET_VOLUME so times are published."""
    base = datetime(2026, 5, 4, 9, 30, 15, 123456, tzinfo=UTC)
    for i in range(n):
        db_session.add(
            Vote(
                law_id=law.id,
                voter_token=f"t{i}",
                choice="for" if i % 2 else "against",
                created_at=base + timedelta(minutes=7 * i),
            )
        )
    db_session.commit()


def _rows(client, law):
    return list(csv.DictReader(io.StringIO(client.get(f"/loi/{law.slug}/export.csv").text)))


def test_export_timestamps_are_rounded(client, open_law, db_session):
    _seed(db_session, open_law)
    rows = _rows(client, open_law)
    assert rows, "the export must not be empty"
    for row in rows:
        # A date, not an instant: no hour, no minute, no microsecond.
        assert row["created_at"].startswith("2026-05-04")
        assert ":" not in row["created_at"]


def test_a_low_volume_votation_publishes_no_times_at_all(client, open_law, db_session):
    """The evolution curve is withheld below the threshold; so is the time column.

    Otherwise the export would hand back exactly what that gate withholds, at any
    resolution the reader chooses.
    """
    settings = get_settings()
    _seed(db_session, open_law, n=settings.min_bucket_volume - 1)
    rows = _rows(client, open_law)
    assert len(rows) == settings.min_bucket_volume - 1
    assert {row["created_at"] for row in rows} == {""}
    # And the file says why, rather than leaving a reader guessing.
    note = client.get(f"/loi/{open_law.slug}/export.json").json()["votes_note"]
    assert "omis" in note
    assert str(settings.min_bucket_volume) in note


def test_the_export_never_reveals_more_than_the_results_page(client, open_law, db_session):
    """The two must agree: whatever the page withholds, the file withholds."""
    settings = get_settings()
    _seed(db_session, open_law, n=settings.min_bucket_volume - 1)
    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "sera affichée lorsque le nombre de votes sera suffisant" in page
    assert {row["created_at"] for row in _rows(client, open_law)} == {""}


def test_export_rows_are_not_in_chronological_order(client, open_law, db_session):
    """Row order must not reconstruct the sequence the votes arrived in."""
    _seed(db_session, open_law, n=60)
    rows = _rows(client, open_law)
    assert len(rows) == 60
    # The votes alternate against/for as they were cast; an id- or time-ordered
    # export would reproduce that pattern exactly.
    order = [r["choice"] for r in rows]
    alternating = ["against" if i % 2 == 0 else "for" for i in range(60)]
    assert order != alternating
    # They are ordered on the published columns and nothing else, so votes sharing
    # a day are grouped by choice — carrying no more than the file already states.
    assert order == sorted(order)


def test_export_is_reproducible(client, open_law, db_session):
    """The same export twice is the same file."""
    _seed(db_session, open_law, n=60)
    first = client.get(f"/loi/{open_law.slug}/export.csv").text
    second = client.get(f"/loi/{open_law.slug}/export.csv").text
    assert first == second


def test_the_json_export_says_what_it_rounded(client, open_law, db_session):
    _seed(db_session, open_law)
    data = client.get(f"/loi/{open_law.slug}/export.json").json()
    # The note no longer names the resolution — it states the guarantee that
    # matters, and the rounding is visible in the rows themselves.
    assert data["votes_note"] == "L'export ne révèle rien de plus que la page de résultats."
    for vote in data["votes"]:
        assert vote["created_at"] == "2026-05-04"


def test_exact_resolution_is_available_but_not_the_default(client, open_law, db_session):
    """An operator may opt in, knowingly — the shipped default does not."""
    settings = get_settings()
    assert settings.export_time_resolution == "day"
    _seed(db_session, open_law)
    previous = settings.export_time_resolution
    settings.export_time_resolution = "exact"
    try:
        stamps = [row["created_at"] for row in _rows(client, open_law)]
        assert any("T09:30:15" in s for s in stamps)
    finally:
        settings.export_time_resolution = previous


def test_a_large_export_is_streamed_not_buffered(client, open_law, db_session):
    """The endpoint is public and unauthenticated: it must not build the file in RAM."""
    _seed(db_session, open_law, n=250)
    with client.stream("GET", f"/loi/{open_law.slug}/export.csv") as response:
        assert response.status_code == 200
        # A streamed body has no precomputed length.
        assert "content-length" not in response.headers
        chunks = list(response.iter_text())
    assert len("".join(chunks).strip().splitlines()) == 251  # header + rows
