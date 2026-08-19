"""A small in-memory rate limiter for the vote endpoint.

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

# address -> timestamps of recent hits, oldest first.
_hits: dict[str, deque[float]] = {}
# Bound the table so a spray of forged addresses cannot exhaust memory.
_MAX_TRACKED = 20_000


def _client_key(request: Request) -> str:
    """The client address, trusting a proxy header only when one is present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


def too_many(request: Request, *, limit: int, window_seconds: int) -> bool:
    """Record this request and report whether the caller is over the limit."""
    if limit <= 0:
        return False
    now = time.monotonic()
    cutoff = now - window_seconds
    key = _client_key(request)

    seen = _hits.get(key)
    if seen is None:
        if len(_hits) >= _MAX_TRACKED:
            _forget_stale(cutoff)
        seen = _hits.setdefault(key, deque())

    while seen and seen[0] < cutoff:
        seen.popleft()
    if len(seen) >= limit:
        return True
    seen.append(now)
    return False


def _forget_stale(cutoff: float) -> None:
    for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
        _hits.pop(key, None)


def reset() -> None:
    """Clear all counters (tests, and a deliberate operational reset)."""
    _hits.clear()
