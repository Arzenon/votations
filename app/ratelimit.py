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

import time
from collections import deque

from fastapi import Request

from app.config import get_settings

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
    key = f"{bucket}|{client_key(request)}"

    _since_sweep += 1
    if _since_sweep >= _SWEEP_EVERY:
        _since_sweep = 0
        _forget_stale(cutoff)

    seen = _hits.get(key)
    if seen is None:
        if len(_hits) >= _MAX_TRACKED:
            _forget_stale(cutoff)
        if len(_hits) >= _MAX_TRACKED:
            # Still full after a sweep: every tracked address is active inside its
            # window, which is a flood rather than traffic. Evict the least
            # recently seen instead of refusing this caller — turning a flood into
            # a site-wide refusal of everyone not already tracked would hand an
            # attacker the outage they were looking for.
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
    seen = _hits.get(f"{bucket}|{client_key(request)}")
    if not seen:
        return False
    cutoff = time.monotonic() - window_seconds
    return sum(1 for hit in seen if hit >= cutoff) >= limit


def _forget_stale(cutoff: float) -> None:
    for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
        _hits.pop(key, None)


def _forget_oldest(fraction: float = 0.1) -> None:
    """Evict the least recently active entries to make room.

    Called only when the table is full of entries that are all still inside their
    window. A whole batch goes at once so a sustained flood does not pay for an
    O(n) scan on every single request.
    """
    if not _hits:
        return
    how_many = max(1, int(len(_hits) * fraction))
    oldest = sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0)[:how_many]
    for key in oldest:
        _hits.pop(key, None)


def reset() -> None:
    """Clear all counters (tests, and a deliberate operational reset)."""
    global _since_sweep
    _hits.clear()
    _since_sweep = 0
