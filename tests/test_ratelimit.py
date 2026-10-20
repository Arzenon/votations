"""In-memory rate limits: votes, admin logins, export capacity."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from markupsafe import escape

from app import ratelimit
from app.config import get_settings
from tests.helpers import admin_headers, basic_auth

# --- Vote rate limiting -------------------------------------------------------


def test_a_burst_of_votes_is_refused(client, open_law, monkeypatch):
    """A burst of votes is refused."""
    from app import ratelimit

    ratelimit.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 3, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [303, 303, 303]
    assert codes[3:] == [429, 429]
    ratelimit.reset()


def test_the_limit_message_does_not_leak_the_threshold(client, open_law, monkeypatch):
    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 429
    assert "Trop de votes" in r.text
    assert "1" not in r.text.split("<h1>")[1].split("</h1>")[0]
    ratelimit.reset()


def test_counters_are_never_persisted():
    """The counters (IP addresses) are only kept in memory."""
    from app import ratelimit

    ratelimit.reset()
    assert ratelimit._hits == {}


# --- Rate-limit key -------------------------------------------------------------
#
# Proxies append to X-Forwarded-For, so the leftmost entries come from the client.


def test_a_forged_forwarded_for_cannot_buy_a_fresh_bucket(client, open_law, monkeypatch):
    """A fake X-Forwarded-For doesn't give a new bucket."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            # Fake left entry each time; the real one is on the right.
            headers={"X-Forwarded-For": f"9.9.9.{i}, 10.0.0.1"},
            follow_redirects=False,
        ).status_code
        for i in range(5)
    ]
    assert codes[:2] == [303, 303]
    assert codes[2:] == [429, 429, 429], "the spoofed left-hand entry must not be the key"


def test_the_real_client_address_still_separates_voters(client, open_law, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    first = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"X-Forwarded-For": "203.0.113.7"},
        follow_redirects=False,
    )
    other = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"X-Forwarded-For": "203.0.113.8"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert other.status_code == 303, "a genuinely different address gets its own budget"


def test_one_ipv6_connection_cannot_vote_from_a_fresh_address_each_time(
    client, open_law, monkeypatch
):
    """Addresses of the same /64 share one budget."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            headers={"X-Forwarded-For": f"2001:db8:1:2::{i}"},
            follow_redirects=False,
        ).status_code
        for i in (1, 2)
    ]
    assert codes == [303, 429]


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("pas-une-adresse", "pas-une-adresse"),
    ],
)
def test_the_key_is_one_connection(monkeypatch, address, key):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1, raising=False)
    request = SimpleNamespace(headers={"x-forwarded-for": address}, client=None)
    assert ratelimit.client_key(request) == key


def test_without_a_declared_proxy_the_header_is_ignored(client, open_law, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            headers={"X-Forwarded-For": f"9.9.9.{i}"},
            follow_redirects=False,
        ).status_code
        for i in range(4)
    ]
    assert codes[2:] == [429, 429]


def test_the_tracking_table_is_bounded(monkeypatch):
    """The table size is capped."""
    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED", 50)

    def request_from(address: str):
        """A fake request with what the limiter reads."""
        return SimpleNamespace(headers={}, client=SimpleNamespace(host=address))

    refused = sum(
        ratelimit.too_many(
            request_from(f"10.0.{i // 256}.{i % 256}"), limit=100, window_seconds=300
        )
        for i in range(400)
    )
    assert len(ratelimit._hits) <= 50
    # Past the cap, old entries are evicted and the caller is still let in.
    assert refused == 0, "a full table must evict, not refuse legitimate voters"
    ratelimit.reset()


# --- Rate limiting across buckets --------------------------------------------


def test_a_sweep_does_not_expire_a_longer_windowed_bucket(client, open_law, monkeypatch):
    """A sweep uses the widest window, so it doesn't reset another bucket's count."""
    settings = get_settings()
    monkeypatch.setattr(settings, "export_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "export_rate_window_seconds", 3600, raising=False)
    monkeypatch.setattr(settings, "vote_rate_window_seconds", 1, raising=False)
    ratelimit.reset()

    for _ in range(2):
        assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 200
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 429

    # Sweep from the short-window bucket.
    monkeypatch.setattr(ratelimit, "_SWEEP_EVERY", 1, raising=False)
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 429


# --- Admin throttle -----------------------------------------------------------


def test_guessing_is_refused_not_merely_counted(client, monkeypatch):
    """Repeated wrong passwords get a 429."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    wrong = basic_auth("admin", "nope")

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429, 429], "past the limit, attempts must be refused"


def test_a_throttled_response_does_not_reveal_a_correct_password(client, monkeypatch):
    """A 429 doesn't reveal whether the password was correct."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 2, raising=False)
    for _ in range(2):
        client.get("/admin", headers=basic_auth("admin", "nope"))

    refused_wrong = client.get("/admin", headers=basic_auth("admin", "still-wrong"))
    refused_right = client.get("/admin", headers=admin_headers())
    assert refused_wrong.status_code == refused_right.status_code == 429
    assert refused_wrong.text == refused_right.text


# --- Administrator brute force ------------------------------------------------


def test_repeated_bad_passwords_are_throttled(client, monkeypatch):
    """Repeated bad passwords are throttled."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_rate_limit", 3, raising=False)
    wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401]

    # The correct password is refused too during the streak.
    assert client.get("/admin", headers=admin_headers()).status_code == 429


def test_a_working_administrator_is_never_slowed_down(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    codes = [client.get("/admin", headers=admin_headers()).status_code for _ in range(10)]
    assert codes == [200] * 10


# --- Exports: concurrency, not just rate -------------------------------------


def test_exports_are_capped_by_how_many_stream_at_once(client, open_law, monkeypatch):
    """Concurrent exports are capped."""
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
    """A finished export frees its slot."""
    ratelimit.reset()
    for _ in range(3):
        assert client.get(f"/loi/{open_law.slug}/export.json").status_code == 200
        assert ratelimit.active_exports() == 0


def test_being_refused_for_capacity_does_not_spend_the_rate_limit(client, open_law, monkeypatch):
    """A capacity refusal doesn't count against the rate limit."""
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


# --- The table is shared by every request thread ------------------------------


def test_the_table_survives_being_swept_while_it_is_written(monkeypatch):
    """Sweeping while other threads insert doesn't raise.

    Without the lock: "dictionary changed size during iteration".
    """
    import threading

    from app import ratelimit as rl

    # Sweep on almost every call.
    monkeypatch.setattr(rl, "_SWEEP_EVERY", 1)
    rl.reset()

    failures: list[BaseException] = []
    stop = threading.Event()

    def hammer(worker: int) -> None:
        try:
            i = 0
            while not stop.is_set():
                request = SimpleNamespace(headers={}, client=SimpleNamespace(host=f"{worker}.{i}"))
                rl.too_many(request, limit=5, window_seconds=300, bucket="vote")
                rl.over_limit(request, limit=5, window_seconds=300, bucket="vote")
                i += 1
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    stop.wait(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not failures, f"concurrent access raised {failures[0]!r}"
    rl.reset()


def test_a_refusal_tells_the_client_when_to_come_back(client, open_law, monkeypatch):
    """A 429 has a Retry-After header."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    assert refused.status_code == 429
    assert refused.headers.get("retry-after") == "60"


def test_a_refusal_does_not_blame_the_reader(client, open_law, monkeypatch):
    """The message mentions that the connection may be shared."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    assert "partagent" in refused.text or escape("n'y êtes") in refused.text


# --- Empty ballots ------------------------------------------------------------


def test_an_empty_ballot_does_not_spend_the_allowance(client, open_law):
    """An empty ballot doesn't count against the limit (shared by everyone on the IP)."""
    settings = get_settings()
    for _ in range(settings.vote_rate_limit + 5):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": ""})

    # Many empty ballots, then a real vote still works.
    response = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert response.status_code in (200, 303)
    assert "Trop de votes" not in response.text


def test_a_flood_of_real_ballots_is_still_refused(client, open_law):
    """Real ballots are still limited."""
    settings = get_settings()
    for _ in range(settings.vote_rate_limit):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert refused.status_code == 429


def test_a_vote_on_a_law_that_just_closed_still_counts(client, closed_law, open_law):
    """A vote on a just-closed law still counts towards the limit."""
    settings = get_settings()
    for _ in range(settings.vote_rate_limit):
        client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert refused.status_code == 429
