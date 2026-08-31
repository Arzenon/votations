"""A small in-memory rate limiter for the vote and admin endpoints.

Deliberately not backed by Redis or a table. The key is a client address, which
is **personal data**: it lives in this process for the length of one window and
is never written to a log, a database, or an export. A restart forgets everything,
which is the intended trade — this exists to make scripted ballot stuffing slow,
not to build a record of who visited.

Counts are per process. With a single app container that is the whole picture;
behind several replicas each holds its own tally, so the effective limit is the
configured one multiplied by the number of replicas.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TypedDict

from fastapi import Request

from app.config import get_settings


class Throttle(TypedDict):
    """One bucket's settings, so a caller can name them once and use them twice.

    Both callers that check before recording — the administration guard and the
    vote route — have to pass *exactly* the same three values to
    :func:`over_limit` and to :func:`too_many`, or the check and the record would
    land in different buckets and neither would do its job. A plain dict reads
    back as ``dict[str, object]``, which cannot be unpacked into their typed
    parameters; this can.
    """

    limit: int
    window_seconds: int
    bucket: str


# "bucket|address" -> timestamps of recent hits, oldest first.
_hits: dict[str, deque[float]] = {}
# Bound the table so a spray of forged addresses cannot exhaust memory.
_MAX_TRACKED = 20_000
# Sweep expired entries every this many recorded hits. Without a periodic sweep
# the table only ever shrinks at the ceiling, so a busy process pays for a full
# scan on *every* new address once it fills — and, worse, refuses everyone it has
# not already seen. Sweeping as we go keeps it far from the ceiling in normal use.
_SWEEP_EVERY = 512
_since_sweep = 0

# The table is shared by every request thread: the routes are sync `def`, so
# Starlette runs each one on its own thread, up to `db_max_threads` of them.
# Both the sweep and the eviction walk the whole dict, and walking it while
# another thread inserts a new address raises "dictionary changed size during
# iteration" — an unhandled 500 on the vote route. Counting a deque that another
# thread is trimming fails the same way.
#
# The window is narrow while the table is small (a fast comprehension is rarely
# preempted) and widens as it grows — that is, it opens up precisely under the
# flood this limiter exists to slow down. The critical section is a handful of
# dict operations, nothing that blocks, and the request is about to spend orders
# of magnitude longer on a database round trip.
#
# Holding it across the read-then-append in `too_many` is a second, smaller fix:
# two simultaneous votes from one address could both see room under the limit.
_hits_lock = threading.Lock()


def client_key(request: Request) -> str:
    """The client address, reading X-Forwarded-For from the *trusted* end.

    Every proxy in the chain appends the address it saw, so the rightmost entries
    are the ones our own infrastructure wrote and the leftmost are whatever the
    caller sent. Taking ``[0]`` therefore hands the rate-limit key to the
    attacker; we count ``trusted_proxy_hops`` back from the right instead, and
    ignore the header entirely when no proxy is declared.
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            if chain:
                # hops=1 → the address our own proxy appended, i.e. the last one.
                index = max(0, len(chain) - hops)
                return chain[index]
    return request.client.host if request.client else "inconnu"


def too_many(request: Request, *, limit: int, window_seconds: int, bucket: str = "vote") -> bool:
    """Record this request and report whether the caller is over the limit."""
    global _since_sweep
    if limit <= 0:
        return False
    now = time.monotonic()
    cutoff = now - window_seconds
    # Both of these read settings and neither touches the table, so they are
    # computed before the lock is taken: the critical section holds nothing it
    # does not need to.
    key = f"{bucket}|{client_key(request)}"

    # The sweep runs over *every* bucket, so it may not use this bucket's cutoff:
    # a vote (five-minute window) would then expire an export entry whose window
    # is an hour, handing back allowance the caller had already spent. The widest
    # configured window is the only cutoff that is safe for all of them.
    stale_before = now - _widest_window()

    with _hits_lock:
        _since_sweep += 1
        if _since_sweep >= _SWEEP_EVERY:
            _since_sweep = 0
            _forget_stale(stale_before)

        seen = _hits.get(key)
        if seen is None:
            if len(_hits) >= _MAX_TRACKED:
                _forget_stale(stale_before)
            if len(_hits) >= _MAX_TRACKED:
                # Still full after a sweep: every tracked address is active inside
                # its window, which is a flood rather than traffic. Evict the least
                # recently seen instead of refusing this caller — turning a flood
                # into a site-wide refusal of everyone not already tracked would
                # hand an attacker the outage they were looking for.
                _forget_oldest()
            seen = _hits.setdefault(key, deque())

        while seen and seen[0] < cutoff:
            seen.popleft()
        if len(seen) >= limit:
            return True
        seen.append(now)
        return False


def over_limit(request: Request, *, limit: int, window_seconds: int, bucket: str = "vote") -> bool:
    """Whether the caller is already over the limit, **without** recording a hit.

    Used where the request itself should not count — a successful admin login
    still has to be refused while that address is in a guessing streak, but must
    not extend the streak by succeeding.
    """
    if limit <= 0:
        return False
    key = f"{bucket}|{client_key(request)}"
    cutoff = time.monotonic() - window_seconds
    with _hits_lock:
        seen = _hits.get(key)
        if not seen:
            return False
        return sum(1 for hit in seen if hit >= cutoff) >= limit


def _widest_window() -> float:
    """The longest window any bucket is configured with.

    An entry is only genuinely dead once it has fallen out of *every* window it
    could belong to, and one table holds all the buckets.
    """
    settings = get_settings()
    return float(
        max(
            settings.vote_rate_window_seconds,
            settings.admin_rate_window_seconds,
            settings.export_rate_window_seconds,
        )
    )


def _forget_stale(cutoff: float) -> None:
    """Drop every entry last seen before ``cutoff``. Caller holds ``_hits_lock``."""
    for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
        _hits.pop(key, None)


def _forget_oldest(fraction: float = 0.1) -> None:
    """Evict the least recently active entries to make room.

    Called only when the table is full of entries that are all still inside their
    window. A whole batch goes at once so a sustained flood does not pay for an
    O(n) scan on every single request. Caller holds ``_hits_lock``.
    """
    if not _hits:
        return
    how_many = max(1, int(len(_hits) * fraction))
    oldest = sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0)[:how_many]
    for key in oldest:
        _hits.pop(key, None)


# --- Concurrency, which is a different question from rate -------------------
#
# An export is the one response on the site whose length is unbounded: it holds
# a server-side cursor, and therefore a pooled database connection, for as long
# as the client takes to read it. The per-address rate limit does not bound that
# — ten downloads from each of three addresses is inside every limit and still
# more connections than the pool has. A slow reader is then indistinguishable
# from an outage, because the pages that only need one quick query cannot get a
# connection either.
#
# So exports are also capped by how many may be *in flight* at once. The count is
# kept by the streaming generator itself, not by the route: a slot taken when the
# response is created and released when it finishes would leak whenever a client
# disconnects in between, and a leaked slot never comes back. Here nothing is
# ever taken that the generator did not enter, and `finally` gives it back.
#
# The consequence is that `slots_free` is a read, not a reservation: several
# requests arriving in the same instant can all see room and all proceed. The cap
# is a guardrail against slow readers piling up, not a hard admission gate, and
# briefly exceeding it by a couple is harmless where leaking a slot is not.
_active_exports = 0
_active_lock = threading.Lock()


def slots_free(*, limit: int) -> bool:
    """Whether another streamed export may start."""
    return _active_exports < limit


@contextmanager
def export_slot() -> Iterator[None]:
    """Count one export as in flight for the duration of its streaming."""
    global _active_exports
    with _active_lock:
        _active_exports += 1
    try:
        yield
    finally:
        with _active_lock:
            _active_exports -= 1


def active_exports() -> int:
    """How many exports are streaming right now (tests, diagnostics)."""
    return _active_exports


def reset() -> None:
    """Clear all counters (tests, and a deliberate operational reset)."""
    global _since_sweep, _active_exports
    with _hits_lock:
        _hits.clear()
        _since_sweep = 0
    with _active_lock:
        _active_exports = 0
