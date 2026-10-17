"""The export contains no identifying information."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app import ratelimit
from app.config import get_settings
from app.crud import results as results_crud
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
# Exact timestamps would let anyone who knows when someone voted find their vote.


def _seed(db_session, law, n=60):
    """Create n votes (by default above MIN_BUCKET_VOLUME)."""
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
        # A date only.
        assert row["created_at"].startswith("2026-05-04")
        assert ":" not in row["created_at"]


def test_a_low_volume_votation_publishes_no_times_at_all(client, open_law, db_session):
    """Below the threshold, the export has no timestamps (like the curve)."""
    settings = get_settings()
    _seed(db_session, open_law, n=settings.min_bucket_volume - 1)
    rows = _rows(client, open_law)
    assert len(rows) == settings.min_bucket_volume - 1
    assert {row["created_at"] for row in rows} == {""}
    # And the file explains why.
    note = client.get(f"/loi/{open_law.slug}/export.json").json()["votes_note"]
    assert "omis" in note
    assert str(settings.min_bucket_volume) in note


def test_the_export_never_reveals_more_than_the_results_page(client, open_law, db_session):
    """The export never shows more than the results page."""
    settings = get_settings()
    _seed(db_session, open_law, n=settings.min_bucket_volume - 1)
    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "sera affichée lorsque le nombre de votes sera suffisant" in page
    assert {row["created_at"] for row in _rows(client, open_law)} == {""}


def test_export_rows_are_not_in_chronological_order(client, open_law, db_session):
    """Row order doesn't reveal the order in which votes arrived."""
    _seed(db_session, open_law, n=60)
    rows = _rows(client, open_law)
    assert len(rows) == 60
    # Votes alternate against/for; sorting by id or time would show that.
    order = [r["choice"] for r in rows]
    alternating = ["against" if i % 2 == 0 else "for" for i in range(60)]
    assert order != alternating
    # Sorted by the published columns only, so grouped by choice within a day.
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
    assert data["votes_note"] == "L'export ne révèle rien de plus que la page de résultats."
    for vote in data["votes"]:
        assert vote["created_at"] == "2026-05-04"


def test_the_json_export_names_its_licence(client, open_law, db_session):
    _seed(db_session, open_law)
    data = client.get(f"/loi/{open_law.slug}/export.json").json()
    assert data["licence"] == {
        "name": "Licence Ouverte 2.0",
        "url": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    }


def test_exact_resolution_is_available_but_not_the_default(client, open_law, db_session):
    """Exact timestamps are possible but not the default."""
    settings = get_settings()
    assert settings.export_time_resolution == "day"
    _seed(db_session, open_law)
    previous = settings.export_time_resolution
    settings.export_time_resolution = "exact"
    try:
        stamps = [row["created_at"] for row in _rows(client, open_law)]
        # Compare instants, since the output is in the display zone.
        moments = {datetime.fromisoformat(s).astimezone(UTC) for s in stamps}
        assert datetime(2026, 5, 4, 9, 30, 15, 123456, tzinfo=UTC) in moments
    finally:
        settings.export_time_resolution = previous


def test_a_large_export_is_streamed_not_buffered(client, open_law, db_session):
    """A large export is streamed, not built in memory."""
    _seed(db_session, open_law, n=250)
    with client.stream("GET", f"/loi/{open_law.slug}/export.csv") as response:
        assert response.status_code == 200
        # A streamed body has no precomputed length.
        assert "content-length" not in response.headers
        chunks = list(response.iter_text())
    assert len("".join(chunks).strip().splitlines()) == 251  # header + rows


# --- Streaming and throttling ---------------------------------------------------


def test_the_json_export_is_valid_and_streamed(client, db_session, open_law):
    for _ in range(5):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    r = client.get(f"/loi/{open_law.slug}/export.json")
    assert r.status_code == 200
    body = json.loads(r.text)
    assert body["law"]["slug"] == open_law.slug
    assert len(body["votes"]) == 5
    assert set(body["votes"][0]) == {"law_id", "choice", "created_at"}
    assert "votes_note" in body


def test_the_json_export_is_valid_when_a_law_has_no_votes(client, open_law):
    body = json.loads(client.get(f"/loi/{open_law.slug}/export.json").text)
    assert body["votes"] == []


def test_exports_are_rate_limited_far_below_voting(client, open_law, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_rate_limit", 2, raising=False)
    ratelimit.reset()
    codes = [client.get(f"/loi/{open_law.slug}/export.csv").status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2:] == [429, 429]


# --- Days in the display zone ------------------------------------------------


def _vote_at(db_session, law, moment: datetime, n: int = 60, tag: str = "a") -> None:
    for i in range(n):
        db_session.add(
            Vote(
                law_id=law.id,
                voter_token=f"tz-{tag}-{i}",
                choice="for",
                created_at=moment + timedelta(seconds=i),
            )
        )
    db_session.commit()


def test_a_vote_just_after_midnight_in_paris_counts_as_that_day(client, open_law, db_session):
    """A vote at 00:30 Paris counts for that day, not the previous one (UTC)."""
    assert get_settings().display_timezone == "Europe/Paris"
    # 2026-05-03 22:30 UTC is 2026-05-04 00:30 in Paris (CEST, +02:00).
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 22, 30, tzinfo=UTC))

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [b.label for b in buckets] == ["2026-05-04"]

    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    assert rows
    assert {row["created_at"] for row in rows} == {"2026-05-04"}


def test_the_export_and_the_curve_agree_on_the_boundary(client, open_law, db_session):
    """The export and the curve use the same day boundaries."""
    # 23:00 and 01:00 Paris: same UTC day, two Paris days.
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 21, 0, tzinfo=UTC), n=30, tag="veille")
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 23, 0, tzinfo=UTC), n=30, tag="apres")

    curve_days = {b.label for b in results_crud.evolution(db_session, open_law.id)}
    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    assert {row["created_at"] for row in rows} == curve_days == {"2026-05-03", "2026-05-04"}


def test_the_export_merges_quiet_days_like_the_curve(client, open_law, db_session, monkeypatch):
    """Votes of a quiet day carry the start of their period, and come sorted by choice."""
    monkeypatch.setattr(get_settings(), "min_cell_count", 10, raising=False)
    _vote_at(db_session, open_law, datetime(2026, 5, 4, 12, 0, tzinfo=UTC), n=50, tag="calme")
    db_session.add(
        Vote(
            law_id=open_law.id,
            voter_token="tz-seul",
            choice="against",
            created_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
    )
    db_session.commit()

    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    assert {row["created_at"] for row in rows} == {"2026-05-04"}, "the lone vote's day leaked"
    assert [row["choice"] for row in rows] == ["against"] + ["for"] * 50


def test_hour_groups_never_cross_the_curve_s_days(client, open_law, db_session, monkeypatch):
    """With hour precision, the hour groups add up to the days on the results page.

    A 23:00 vote used to share an hour group with the next morning: the day's
    figures minus its 10:00 group then gave back that one late "Contre".
    """
    monkeypatch.setattr(get_settings(), "export_time_resolution", "hour", raising=False)
    monkeypatch.setattr(get_settings(), "min_cell_count", 10, raising=False)
    paris = ZoneInfo("Europe/Paris")
    plan = [(4, 10, "for", 14), (4, 23, "against", 1), (5, 1, "for", 9), (5, 12, "for", 30)]
    for day, hour, choice, n in plan:
        for i in range(n):
            db_session.add(
                Vote(
                    law_id=open_law.id,
                    voter_token=f"h{day}-{hour}-{i}",
                    choice=choice,
                    created_at=datetime(2026, 5, day, hour, 0, tzinfo=paris),
                )
            )
    db_session.commit()

    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    groups: dict[str, dict[str, int]] = {}
    for row in rows:
        cell = groups.setdefault(row["created_at"], {"for": 0, "against": 0, "abstain": 0})
        cell[row["choice"]] += 1
    # The lone 23:00 vote joins its own day's group, not the next morning's.
    assert groups == {
        "2026-05-04T10:00:00+0200": {"for": 14, "against": 1, "abstain": 0},
        "2026-05-05T01:00:00+0200": {"for": 39, "against": 0, "abstain": 0},
    }
    days = {b.label: b.counts for b in results_crud.evolution(db_session, open_law.id)}
    assert days == {
        "2026-05-04": {"for": 14, "against": 1, "abstain": 0},
        "2026-05-05": {"for": 39, "against": 0, "abstain": 0},
    }


# --- Streaming is batched, not one message per vote ---------------------------


def test_an_export_hands_over_bufferfuls_not_one_chunk_per_vote(db_session, open_law):
    """The export is sent in chunks, not one per vote.

    Each yield costs a thread pool round trip and an ASGI message.
    """
    import anyio

    from app.routes.public.exports import _CHUNK_CHARS, csv_response

    _seed(db_session, open_law, n=4000)

    # One ASGI message per item of body_iterator.
    chunks: list[str] = []

    async def drain() -> None:
        async for chunk in csv_response(db_session, open_law).body_iterator:
            chunks.append(chunk)

    anyio.run(drain)

    rows = "".join(chunks).strip().splitlines()
    assert len(rows) == 4001, "header + every vote, whatever the chunking"
    assert len(chunks) < 100, f"{len(chunks)} chunks for 4000 votes, batching is gone"
    assert max(len(c) for c in chunks) <= _CHUNK_CHARS + 200, (
        "a chunk grew past the buffer - the point was to bound memory too"
    )


# --- HEAD asks whether the file is there, not for the file --------------------


def _scans(monkeypatch) -> list[int]:
    """Count how many times the export scanned the votes."""
    from app.routes.public import exports

    seen: list[int] = []
    real = exports.results_crud.iter_export_rows

    def counting(*args, **kwargs):
        seen.append(1)
        yield from real(*args, **kwargs)

    monkeypatch.setattr(exports.results_crud, "iter_export_rows", counting)
    return seen


def test_head_on_an_export_answers_without_scanning_the_votes(client, open_law, monkeypatch):
    """HEAD on an export doesn't scan the votes."""
    scans = _scans(monkeypatch)
    for suffix in ("csv", "json"):
        r = client.head(f"/loi/{open_law.slug}/export.{suffix}")
        assert r.status_code == 200, suffix
        assert "attachment" in r.headers["content-disposition"], suffix
        assert open_law.slug in r.headers["content-disposition"], suffix
    assert scans == [], "a HEAD built the whole export to throw it away"


def test_get_still_scans_and_still_returns_the_rows(client, open_law, db_session, monkeypatch):
    """GET still scans and returns the rows."""
    db_session.add(Vote(law_id=open_law.id, voter_token="t", choice="for"))
    db_session.commit()

    scans = _scans(monkeypatch)
    body = client.get(f"/loi/{open_law.slug}/export.csv").text
    assert len(scans) == 1
    assert len(body.strip().splitlines()) == 2, "header + the one vote"


def test_head_on_a_page_is_unaffected(client, open_law):
    """HEAD on other pages is unaffected."""
    for path in ("/", f"/loi/{open_law.slug}", "/robots.txt"):
        assert client.head(path).status_code == 200, path
