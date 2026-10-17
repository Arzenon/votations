"""In-memory rate limiting for votes, admin logins and exports.

Not stored in Redis or the database on purpose: the key is an IP address,
which is personal data. It stays in memory for one window and is never logged.
Counts are per process, so with several replicas the effective limit is
multiplied.
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
    """Bucket settings, unpacked into both `over_limit` and `too_many`."""

    limit: int
    window_seconds: int
    bucket: str


# "bucket|address" -> timestamps of recent hits, oldest first.
_hits: dict[str, deque[float]] = {}
# Cap on tracked addresses, so a flood of fake ones can't exhaust memory.
_MAX_TRACKED = 20_000
# Sweep expired entries every N hits, to stay well below the cap.
_SWEEP_EVERY = 512
_since_sweep = 0

# Sync routes run in a thread pool, so the table needs a lock. Iterating it
# while another thread inserts raises "dictionary changed size during
# iteration".
_hits_lock = threading.Lock()


def client_key(request: Request) -> str:
    """The client IP, counting ``trusted_proxy_hops`` from the right of X-Forwarded-For.

    The leftmost entries are set by the client and can't be trusted.
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            if chain:
                # hops=1: the last entry, added by our proxy.
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

    # The sweep covers all buckets, so use the widest window.
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
                # Still full: evict the least recent entries rather than refuse
                # every new visitor.
                _forget_oldest()
            seen = _hits.setdefault(key, deque())

        while seen and seen[0] < cutoff:
            seen.popleft()
        if len(seen) >= limit:
            return True
        seen.append(now)
        return False


def over_limit(request: Request, *, limit: int, window_seconds: int, bucket: str = "vote") -> bool:
    """Whether the caller is over the limit, without recording a hit.

    Used for admin logins: a correct password is still refused during a
    brute-force streak, but doesn't count as a failure.
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
    """The longest configured window across buckets."""
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
    """Evict a batch of the least recently active entries. Caller holds ``_hits_lock``."""
    if not _hits:
        return
    how_many = max(1, int(len(_hits) * fraction))
    oldest = sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0)[:how_many]
    for key in oldest:
        _hits.pop(key, None)


# Concurrent exports. Each one holds a DB connection while the client reads,
# so slow clients could exhaust the pool. The generator itself takes the slot
# (released in `finally`), so a disconnecting client can't leak it.
# `slots_free` is only a check, not a reservation: the cap can be briefly
# exceeded.
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
    """Number of exports currently streaming."""
    return _active_exports


def reset() -> None:
    """Clear all counters (tests, manual reset)."""
    global _since_sweep, _active_exports
    with _hits_lock:
        _hits.clear()
        _since_sweep = 0
    with _active_lock:
        _active_exports = 0
