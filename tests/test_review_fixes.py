"""Behaviour introduced by the August 2026 review.

Each test names the thing that was wrong, so a regression reads as a sentence
rather than as a diff.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from itsdangerous import URLSafeSerializer

from app import profile_cookie, ratelimit
from app.config import Settings, get_settings
from app.crud import laws as laws_crud
from app.models import DemographicTally, Law, LawRevision


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin() -> dict:
    s = get_settings()
    return {**_basic(s.admin_username, s.admin_password), "Origin": "http://testserver"}


def _law(db, **kwargs) -> Law:
    defaults = {
        "slug": "loi-x",
        "title": "Loi X",
        "published": True,
        "opens_at": datetime.now(UTC) - timedelta(hours=1),
        "closes_at": datetime.now(UTC) + timedelta(hours=24),
    }
    law = Law(**{**defaults, **kwargs})
    db.add(law)
    db.commit()
    db.refresh(law)
    return law


# --- A1: the administrator throttle actually throttles -------------------------


def test_guessing_is_refused_not_merely_counted(client, monkeypatch):
    """Wrong passwords used to return 401 for ever, at network speed."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    wrong = _basic("admin", "nope")

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429, 429], "past the limit, attempts must be refused"


def test_a_throttled_response_does_not_reveal_a_correct_password(client, monkeypatch):
    """429-only-on-success turned the throttle into an oracle."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 2, raising=False)
    for _ in range(2):
        client.get("/admin", headers=_basic("admin", "nope"))

    refused_wrong = client.get("/admin", headers=_basic("admin", "still-wrong"))
    refused_right = client.get("/admin", headers=_admin())
    assert refused_wrong.status_code == refused_right.status_code == 429
    assert refused_wrong.text == refused_right.text


def test_a_working_administrator_is_never_slowed_down(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    assert [client.get("/admin", headers=_admin()).status_code for _ in range(10)] == [200] * 10


# --- A2: non-ASCII credentials ------------------------------------------------


def test_an_accented_credential_is_refused_not_a_crash(client):
    """Starlette rejects the header before this, but the guard must be safe alone."""
    r = client.get("/admin", headers=_basic("admin", "mot-de-passé"))
    assert r.status_code == 401


def test_comparing_an_accented_password_does_not_raise():
    """`secrets.compare_digest` on str raises TypeError for non-ASCII.

    With a non-ASCII ADMIN_PASSWORD configured, every login attempt used to be an
    unhandled 500 rather than a refusal — the administrator was locked out with no
    indication why.
    """
    from app.routes.admin.guard import _matches

    assert _matches("admin", "clé-très-sûre") is False
    assert _matches("clé-très-sûre", "clé-très-sûre") is True


def test_a_non_ascii_admin_password_is_refused_at_boot():
    """HTTP Basic cannot carry it here, so it could only ever lock the admin out."""
    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_password="clé-très-sûre")

    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_username="administratrice-éé")


# --- A3: the profile cookie is scoped to one law ------------------------------


def _cells(db, law_id) -> dict[tuple[str, str, str], int]:
    rows = db.query(DemographicTally).filter_by(law_id=law_id).all()
    return {(r.dimension, r.category, r.choice): r.count for r in rows}


def test_a_profile_given_on_one_law_never_moves_another_laws_counters(client, db_session):
    """The cookie held one profile for the whole site while counters are per-law.

    Voting 18-24 on A then 65+ on B, then changing the vote on A, used to
    decrement A's 65+ cell — other people's votes — and strand the real 18-24
    contribution under the old choice.
    """
    a = _law(db_session, slug="loi-a", title="Loi A")
    b = _law(db_session, slug="loi-b", title="Loi B")

    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    # Somebody else's vote, so a stray decrement would be visible rather than clamped.
    client_cells_before = _cells(db_session, a.id)
    assert client_cells_before == {("age", "18-24", "for"): 1}

    client.post(f"/loi/{a.slug}/vote", data={"choice": "against"})

    after = _cells(db_session, a.id)
    assert after.get(("age", "18-24", "against")) == 1, "the voter's own cell should move"
    assert after.get(("age", "18-24", "for"), 0) == 0
    assert after.get(("age", "65+", "against"), 0) == 0, "law B's category must not appear on A"
    # And law B is untouched by the change made on law A.
    assert _cells(db_session, b.id) == {("age", "65+", "for"): 1}


def test_the_cookie_remembers_several_laws_at_once(client, db_session):
    a = _law(db_session, slug="loi-c", title="Loi C")
    b = _law(db_session, slug="loi-d", title="Loi D")
    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    from types import SimpleNamespace

    stored = client.cookies.get(profile_cookie.COOKIE_NAME)
    request = SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: stored})
    assert profile_cookie.read(request, a.id) == {"age": "18-24"}
    assert profile_cookie.read(request, b.id) == {"age": "65+"}


def test_the_cookie_cannot_grow_without_bound():
    """A browser that votes on hundreds of laws must not exceed the 4 kB limit."""
    from types import SimpleNamespace

    response = SimpleNamespace(set_cookie=lambda *a, **k: captured.update(value=a[1]))
    captured: dict = {}
    store: dict = {}
    serializer = URLSafeSerializer(get_settings().secret_key, salt="voter-profile")

    for law_id in range(400):
        store[str(law_id)] = {"age": "18-24", "sexe": "femme", "region": "Île-de-France"}
    request = SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: serializer.dumps(store)})

    profile_cookie.write(request, response, 999, {"age": "25-34"})
    assert len(captured["value"].encode()) <= 4000


# --- A4 / E7: the configuration refuses values that used to fail open ---------


def test_a_typo_in_the_export_resolution_is_refused_at_boot():
    """`EXPORT_TIME_RESOLUTION=days` silently published exact vote timestamps."""
    with pytest.raises(ValueError):
        Settings(export_time_resolution="days")


def test_an_unknown_timezone_is_refused_rather_than_silently_utc():
    with pytest.raises(ValueError):
        Settings(display_timezone="Europe/Paris-ish")


def test_a_negative_anonymity_threshold_is_refused():
    with pytest.raises(ValueError):
        Settings(min_cell_count=0)


def test_production_refuses_a_defeated_anonymity_threshold():
    from app.config import InsecureConfiguration, check_production_config

    settings = Settings(
        environment="production",
        secret_key="x" * 48,
        admin_password="un-mot-de-passe-solide",
        cookie_secure=True,
        min_cell_count=1,
    )
    with pytest.raises(InsecureConfiguration, match="MIN_CELL_COUNT"):
        check_production_config(settings)


# --- A5 / A7: headers the app sets for itself --------------------------------


def test_the_law_page_is_never_stored_in_a_shared_cache(client, open_law):
    """It renders the visitor's own ballot."""
    r = client.get(f"/loi/{open_law.slug}")
    assert r.headers["cache-control"] == "private, no-store"


def test_the_admin_is_never_cached(client):
    r = client.get("/admin", headers=_admin())
    assert "no-store" in r.headers["cache-control"]


def test_the_app_sets_its_own_security_headers(client):
    """They used to exist only in the Caddyfile; another proxy meant none at all."""
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]


def test_static_assets_are_cached_hard_and_fingerprinted(client):
    from app.templating import asset_url

    url = asset_url("styles.css")
    assert "?v=" in url
    r = client.get(url)
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]


# --- A8: the exports stream and are throttled --------------------------------


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


# --- B1 / B4 / B5: the form refuses instead of failing quietly ---------------


def test_malformed_argument_json_is_refused_not_silently_wiped(client, db_session):
    law = _law(db_session, slug="loi-args", arguments_for=[{"text": "gardé"}])
    r = client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
        data={"title": "Loi X", "arguments_for": '[{"text": "cassé",}]'},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "JSON illisible" in r.text
    db_session.refresh(law)
    assert law.arguments_for == [{"text": "gardé"}], "the existing arguments must survive"


def test_a_closing_time_before_the_opening_is_refused(client):
    r = client.post(
        "/admin/lois",
        headers=_admin(),
        data={
            "title": "Loi impossible",
            "opens_at": "2026-09-01T10:00",
            "closes_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "postérieure" in r.text


def test_an_over_long_title_is_a_message_not_a_500(client):
    r = client.post(
        "/admin/lois",
        headers=_admin(),
        data={"title": "x" * 501},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "limite" in r.text


def test_an_empty_title_is_refused(client):
    r = client.post("/admin/lois", headers=_admin(), data={"title": "   "}, follow_redirects=False)
    assert r.status_code == 400


# --- B2: the public record of edits made after publication -------------------


def test_editing_a_published_law_is_recorded_and_shown(client, db_session):
    law = _law(db_session, slug="loi-rev", summary_text="Version initiale.")
    client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
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
    law = _law(db_session, slug="loi-brouillon", published=False, summary_text="a")
    client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
        data={"title": law.title, "summary_text": "b"},
        follow_redirects=False,
    )
    assert db_session.query(LawRevision).filter_by(law_id=law.id).count() == 0


def test_an_unmodified_law_says_so_plainly(client, open_law):
    page = client.get(f"/loi/{open_law.slug}/resultats")
    assert "Aucune modification depuis la publication" in page.text


def test_the_law_page_points_at_the_history_when_there_is_one(client, db_session):
    law = _law(db_session, slug="loi-modifiee", summary_text="a")
    laws_crud.update(db_session, law, summary_text="b")
    page = client.get(f"/loi/{law.slug}")
    assert "historique des modifications" in page.text.lower()


# --- B3: archiving replaces deletion -----------------------------------------


def test_archiving_hides_a_law_without_destroying_its_votes(client, db_session, open_law):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})

    r = client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False
    )
    assert r.status_code == 303

    assert client.get(f"/loi/{open_law.slug}").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/resultats").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 404
    assert open_law.slug not in client.get("/").text

    db_session.refresh(open_law)
    assert open_law.archived_at is not None
    from app.crud import results as results_crud

    assert results_crud.count_votes(db_session, open_law.id) == 1, "votes are kept"


def test_an_archived_law_can_be_restored(client, db_session, open_law):
    client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
    client.post(f"/admin/lois/{open_law.id}/restaurer", headers=_admin(), follow_redirects=False)
    assert client.get(f"/loi/{open_law.slug}").status_code == 200


def test_deleting_asks_on_a_page_the_server_renders(client, open_law):
    """The confirmation used to be a JS attribute; with JS off it did nothing."""
    client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
    page = client.get(f"/admin/lois/{open_law.id}/supprimer", headers=_admin())
    assert page.status_code == 200
    assert "<form" in page.text
    assert "irréversible" in page.text


def test_a_law_cannot_be_purged_before_it_is_archived(client, db_session, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer", headers=_admin(), follow_redirects=False
    )
    assert r.status_code == 409
    assert db_session.get(Law, open_law.id) is not None


def test_purging_an_archived_law_removes_it_for_good(client, db_session, open_law):
    law_id = open_law.id
    client.post(f"/admin/lois/{law_id}/archiver", headers=_admin(), follow_redirects=False)
    r = client.post(f"/admin/lois/{law_id}/supprimer", headers=_admin(), follow_redirects=False)
    assert r.status_code == 303
    assert db_session.get(Law, law_id) is None


# --- B7 / C8: cheaper public pages -------------------------------------------


def test_an_absurd_page_number_is_clamped(client, db_session):
    _law(db_session, slug="loi-p", title="Loi paginée")
    r = client.get("/recherche?page=999999999")
    assert r.status_code == 200
    assert "Page 1 / 1" in r.text or "Loi paginée" in r.text


def test_an_open_law_page_does_not_count_the_votes(client, open_law, db_session):
    """The tally is only rendered once voting has closed."""
    from sqlalchemy import event

    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", record)
    try:
        client.get(f"/loi/{open_law.slug}")
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", record)

    tallies = [s for s in seen if "count" in s.lower() and "votes" in s.lower()]
    assert not any("group by" in s.lower() for s in tallies)


# --- C1: htmx only where it is used ------------------------------------------


def test_the_home_page_does_not_load_htmx(client):
    assert "htmx" not in client.get("/").text


def test_the_law_page_does_load_htmx(client, open_law):
    assert "htmx" in client.get(f"/loi/{open_law.slug}").text


# --- D1: the draft banner stops nagging, but only after it has been seen -------


def test_a_failed_draft_is_shown_once_then_cleared(client, db_session):
    """`clear_draft_state` existed but was never called: a failure nagged for ever."""
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

    first = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin())
    assert "introuvable" in first.text, "the administrator must see why it failed"

    second = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin())
    assert "introuvable" not in second.text, "and must not be told again for ever"


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

    page = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin())
    assert "en cours de génération" in page.text
    db_session.refresh(item)
    assert item.draft_status == "en_cours"
