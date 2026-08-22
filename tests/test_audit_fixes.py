"""Regressions for the second review pass.

Each test here names the behaviour that was wrong, not the code that was
changed: the point of the file is that the *symptom* cannot come back.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.config import get_settings
from app.crud import results as results_crud
from app.db import include_object
from app.main import app
from app.models import Vote

# --- Migration drift ---------------------------------------------------------


def test_conditionally_created_indexes_are_not_compared():
    """Both hand-written indexes are excluded from autogenerate, for two reasons.

    `ix_laws_fts` is a functional index Alembic cannot compare; the trigram index
    exists only where `pg_trgm` could be installed. Comparing either made the
    drift check report a difference on a schema that was perfectly correct — and
    a drift check that cries wolf is one nobody reads.
    """
    for name in ("ix_laws_fts", "ix_laws_official_ref_trgm"):
        assert not include_object(None, name, "index", True, None)
    # Anything else is still compared, or the check would protect nothing.
    assert include_object(None, "ix_laws_created_at", "index", True, None)
    assert include_object(None, "laws", "table", True, None)


# --- Security headers --------------------------------------------------------


def test_an_unhandled_error_still_carries_the_security_headers(monkeypatch):
    """500s are served above our middleware and used to arrive bare.

    Caddy puts the CSP back; nothing puts `no-store` back. That left the one page
    most likely to be generated under stress as the only cacheable one.
    """
    from app.crud import laws as laws_crud

    def boom(*_args, **_kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(laws_crud, "list_published", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/", headers={"Accept": "text/html"})

    assert r.status_code == 500
    assert r.headers["cache-control"] == "private, no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # And it must still be a page of this site, not a stack trace.
    assert "panne simulée" not in r.text


def test_hsts_is_sent_only_where_the_site_is_already_https():
    """Pinning a developer's browser to https for a year is not a kindness."""
    from app.main import default_headers

    settings = get_settings()
    assert not settings.cookie_secure, "the suite runs without TLS"
    assert not any(name == "Strict-Transport-Security" for name, _ in default_headers())

    previous = settings.cookie_secure
    settings.cookie_secure = True
    try:
        sent = dict(default_headers())
        assert "max-age=31536000" in sent["Strict-Transport-Security"]
    finally:
        settings.cookie_secure = previous


def test_a_normal_response_carries_no_hsts_in_development(client):
    assert "strict-transport-security" not in client.get("/").headers


# --- Rate limiting across buckets --------------------------------------------


def test_a_sweep_does_not_expire_a_longer_windowed_bucket(client, open_law, monkeypatch):
    """One table holds every bucket, so a sweep must use the widest window.

    Sweeping with the *calling* bucket's cutoff let a burst of votes hand back
    export allowance that had already been spent.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "export_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "export_rate_window_seconds", 3600, raising=False)
    monkeypatch.setattr(settings, "vote_rate_window_seconds", 1, raising=False)
    ratelimit.reset()

    for _ in range(2):
        assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 200
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 429

    # Force a sweep from the short-windowed bucket. It must not rescue the export.
    monkeypatch.setattr(ratelimit, "_SWEEP_EVERY", 1, raising=False)
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 429


# --- Exports: concurrency, not just rate -------------------------------------


def test_exports_are_capped_by_how_many_stream_at_once(client, open_law, monkeypatch):
    """An export holds a database connection for as long as the client reads."""
    monkeypatch.setattr(get_settings(), "export_max_concurrent", 1, raising=False)
    ratelimit.reset()

    with ratelimit.export_slot():  # one already in flight
        assert ratelimit.active_exports() == 1
        r = client.get(f"/loi/{open_law.slug}/export.csv")
        assert r.status_code == 503
        assert r.headers["retry-after"] == "60"

    assert ratelimit.active_exports() == 0
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 200


def test_a_finished_export_gives_its_slot_back(client, open_law, db_session):
    """A leaked slot never comes back, so the count is kept by the generator."""
    ratelimit.reset()
    for _ in range(3):
        assert client.get(f"/loi/{open_law.slug}/export.json").status_code == 200
        assert ratelimit.active_exports() == 0


def test_being_refused_for_capacity_does_not_spend_the_rate_limit(client, open_law, monkeypatch):
    """Refusing someone because the server is busy must not also cost them."""
    settings = get_settings()
    monkeypatch.setattr(settings, "export_max_concurrent", 1, raising=False)
    monkeypatch.setattr(settings, "export_rate_limit", 2, raising=False)
    ratelimit.reset()

    with ratelimit.export_slot():
        for _ in range(5):
            assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 503

    # The allowance is untouched: two downloads still work.
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 200
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 200


# --- Days are days where the reader lives ------------------------------------


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
    """Truncating in UTC filed 00:30 Paris under the previous day.

    Visibly wrong on a curve headed « jour par jour », and wrong in the export
    too — which must round on the same boundaries as the curve, or it partitions
    the votes a second, different way.
    """
    assert get_settings().display_timezone == "Europe/Paris"
    # 2026-05-03 22:30 UTC is 2026-05-04 00:30 in Paris (CEST, +02:00).
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 22, 30, tzinfo=UTC))

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [b.label for b in buckets] == ["2026-05-04"]

    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    assert rows
    assert {row["created_at"] for row in rows} == {"2026-05-04"}


def test_the_export_and_the_curve_agree_on_the_boundary(client, open_law, db_session):
    """Whatever the rule is, both sides must apply the same one."""
    # 23:00 and 01:00 Paris: the same UTC day, two different days where the
    # reader lives. Both sides must land on the same pair.
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 21, 0, tzinfo=UTC), n=30, tag="veille")
    _vote_at(db_session, open_law, datetime(2026, 5, 3, 23, 0, tzinfo=UTC), n=30, tag="apres")

    curve_days = {b.label for b in results_crud.evolution(db_session, open_law.id)}
    rows = list(csv.DictReader(io.StringIO(client.get(f"/loi/{open_law.slug}/export.csv").text)))
    assert {row["created_at"] for row in rows} == curve_days == {"2026-05-03", "2026-05-04"}


# --- A refused vote must say so, with JavaScript on as well as off ------------

HX = {"HX-Request": "true"}


def _is_whole_page(text: str) -> bool:
    return "<!DOCTYPE html>" in text


def test_htmx_vote_on_a_law_that_just_closed_returns_a_ballot_not_a_page(client, closed_law):
    """htmx follows a 303 and swaps whatever it lands on into `#ballot`.

    So the redirect nested the entire site — header, disclaimer, footer — inside
    the ballot's div.
    """
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert 'id="ballot"' in r.text
    assert "vient de se clore" in r.text


def test_htmx_vote_without_a_choice_explains_itself(client, open_law):
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": ""}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert "Choisissez" in r.text


def test_htmx_vote_over_the_rate_limit_is_not_silent(client, open_law, monkeypatch):
    """htmx discards the body of a 4xx, so the button appeared to do nothing."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert "Trop de votes" in r.text


def _checked_choice(html: str) -> str | None:
    """Which radio the rendered ballot has pre-selected, if any."""
    match = re.search(r'value="(\w+)"[^>]*\bchecked\b', html)
    return match.group(1) if match else None


def test_a_refused_vote_keeps_the_choice_already_recorded(client, open_law, monkeypatch):
    """A refusal must not silently clear the radio the voter had chosen."""
    cast = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert _checked_choice(cast.text) == "against"

    monkeypatch.setattr(get_settings(), "vote_rate_limit", 0, raising=False)
    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    # The first attempt in the window is allowed and spends the allowance; the
    # second is refused, and is the one whose ballot has to be right.
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "abstain"}, headers=HX)

    assert "Trop de votes" in refused.text
    assert _checked_choice(refused.text) == "for", (
        "the refusal must show what is on file, not the rejected attempt"
    )


def test_without_htmx_a_refused_vote_still_gets_the_ordinary_answer(client, closed_law, open_law):
    """The fragment is an addition, not a replacement: no JavaScript, no change."""
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/loi/{closed_law.slug}"

    r = client.post("/loi/inconnue/vote", data={"choice": "for"})
    assert r.status_code == 404


def test_an_unreadable_form_field_is_a_page_not_json(client):
    """FastAPI's own handler answers a browser form post with pydantic JSON."""
    r = client.post(
        "/admin/lois",
        headers={"Accept": "text/html", **_admin_auth()},
        data={"title": "Loi", "import_id": "pas-un-nombre"},
    )
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "Saisie invalide" in r.text
    # The pydantic detail names internal field paths; it must not be echoed.
    assert "import_id" not in r.text


def _admin_auth() -> dict:
    import base64

    settings = get_settings()
    raw = f"{settings.admin_username}:{settings.admin_password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


# --- The AI corpus is measured, not just trimmed ------------------------------


def test_an_oversized_debate_corpus_is_refused_before_it_is_billed():
    """`generate_draft` measured its input; `select_arguments` only guessed."""
    from app.ai import AiDraftError, select_arguments
    from app.debates import Speech
    from app.legifrance import LawText

    class _Client:
        class messages:  # mirrors the SDK's client.messages.* shape
            @staticmethod
            def count_tokens(**_kw):
                return type("R", (), {"input_tokens": 10_000_000})()

            @staticmethod
            def parse(**_kw):  # pragma: no cover - must never be reached
                raise AssertionError("the model was called despite the size guard")

    law = LawText(
        nor="ABCX2600001L",
        law_code="2026-1",
        title="Loi",
        nature="LOI",
        signed_on=None,
        eli_url=None,
    )
    speeches = [
        Speech(
            speech_id="s1",
            speaker="Mme A",
            quality="",
            chamber="Assemblée nationale",
            section="Discussion générale",
            spoken_on=None,
            text="x" * 500,
            url="https://an.fr/cr",
        )
    ]
    with pytest.raises(AiDraftError, match="trop volumineux"):
        select_arguments(law, speeches, client=_Client())
