"""The in-memory throttles: votes, administrator logins, and export capacity.

One table, several buckets, shared by every request thread — so this file is as
much about the bookkeeping staying correct under concurrency as about the limits
themselves.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

from markupsafe import escape

from app import ratelimit
from app.config import get_settings
from tests.helpers import admin_headers, basic_auth

# --- Vote rate limiting -------------------------------------------------------


def test_a_burst_of_votes_is_refused(client, open_law, monkeypatch):
    """A fresh cookie is a fresh voter, so bursts are the cheapest attack."""
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
    """IP addresses are personal data: they live in this process and nowhere else."""
    from app import ratelimit

    ratelimit.reset()
    assert ratelimit._hits == {}


# --- Rate-limit keying --------------------------------------------------------
#
# The limiter keys on a client address. Reverse proxies *append* to
# X-Forwarded-For, so the leftmost entries are whatever the caller wrote: reading
# from that end handed the key to the attacker and made the whole control a no-op.


def test_a_forged_forwarded_for_cannot_buy_a_fresh_bucket(client, open_law, monkeypatch):
    """The attack: one new fake address per request, and the limit never bites."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            # The caller invents a different left-hand entry every time; our own
            # proxy appended the real one on the right.
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
    """A flood of forged keys must not grow the table without bound."""
    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED", 50)

    def request_from(address: str):
        """The two attributes the limiter reads off a request."""
        return SimpleNamespace(headers={}, client=SimpleNamespace(host=address))

    refused = sum(
        ratelimit.too_many(
            request_from(f"10.0.{i // 256}.{i % 256}"), limit=100, window_seconds=300
        )
        for i in range(400)
    )
    assert len(ratelimit._hits) <= 50
    # Past the cap the least recently active entries are evicted, and the caller
    # is still admitted. Refusing instead — which is what this used to do — turned
    # a flood into a site-wide outage for every address not already tracked, which
    # is precisely the outcome an attacker was after.
    assert refused == 0, "a full table must evict, not refuse legitimate voters"
    ratelimit.reset()


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


# --- A1: the administrator throttle actually throttles -------------------------


def test_guessing_is_refused_not_merely_counted(client, monkeypatch):
    """Wrong passwords used to return 401 for ever, at network speed."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    wrong = basic_auth("admin", "nope")

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429, 429], "past the limit, attempts must be refused"


def test_a_throttled_response_does_not_reveal_a_correct_password(client, monkeypatch):
    """429-only-on-success turned the throttle into an oracle."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 2, raising=False)
    for _ in range(2):
        client.get("/admin", headers=basic_auth("admin", "nope"))

    refused_wrong = client.get("/admin", headers=basic_auth("admin", "still-wrong"))
    refused_right = client.get("/admin", headers=admin_headers())
    assert refused_wrong.status_code == refused_right.status_code == 429
    assert refused_wrong.text == refused_right.text


# --- Administrator brute force ------------------------------------------------


def test_repeated_bad_passwords_are_throttled(client, monkeypatch):
    """A static credential pair with no lockout is guessable at network speed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_rate_limit", 3, raising=False)
    wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401]

    # And the correct password is refused too while that address is guessing,
    # so the throttle cannot be sidestepped by landing on the right one.
    assert client.get("/admin", headers=admin_headers()).status_code == 429


def test_a_working_administrator_is_never_slowed_down(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    codes = [client.get("/admin", headers=admin_headers()).status_code for _ in range(10)]
    assert codes == [200] * 10


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


# --- The table is shared by every request thread ------------------------------


def test_the_table_survives_being_swept_while_it_is_written(monkeypatch):
    """The sweep used to walk the table while another thread inserted into it.

    The routes are sync `def`, so Starlette runs each one on its own thread and
    they all reach for one module-level dict. Both the periodic sweep and the
    eviction iterate it, and iterating a dict another thread is inserting into
    raises "dictionary changed size during iteration" — a 500 on the vote route,
    arriving precisely when the table is large, which is to say under the flood
    the limiter exists to slow down.
    """
    import threading

    from app import ratelimit as rl

    # Sweep on almost every call, so the window this is about is wide open.
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
    """A 429 with no `Retry-After` invites an immediate retry.

    Which is the one thing neither the visitor nor the server wants — and the
    limit is keyed on an address that a mobile carrier shares between thousands
    of people, so the client retrying is very often not the one that spent it.
    """
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    assert refused.status_code == 429
    assert refused.headers.get("retry-after") == "60"


def test_a_refusal_does_not_blame_the_reader(client, open_law, monkeypatch):
    """The message has to leave room for the very likely case: a shared address."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    assert "partagent" in refused.text or escape("n'y êtes") in refused.text
