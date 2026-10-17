"""Shared HTTP fetching and JSON helpers for the French open data sources."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

# Fetches a URL as bytes. Injected in tests.
Getter = Callable[[str], bytes]

_CHUNK = 64 * 1024


class DeadlineExceeded(RuntimeError):
    """The caller's overall time budget is spent.

    Separate from the per-source errors, which callers catch to skip one
    document and continue. Once the budget is spent, every following request
    would fail too, so this one should stop the loop.
    """


def fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    error: type[Exception],
    headers: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> bytes:
    """GET ``url``, raising ``error`` (the caller's exception class) on failure.

    ``max_bytes`` is checked while streaming, since Content-Length may be
    missing. Peak memory is about twice that, because of the final join.

    ``deadline`` caps the total duration in seconds. ``timeout`` alone is per
    socket operation, so a server sending one byte at a time could stall
    forever.
    """
    if deadline is not None:
        if deadline <= 0:
            raise DeadlineExceeded(f"délai épuisé avant même la requête : {url}")
        timeout = min(timeout, deadline)
    started = time.monotonic()
    try:
        with httpx.stream(
            "GET", url, timeout=timeout, follow_redirects=True, headers=headers
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes(_CHUNK):
                if deadline is not None and time.monotonic() - started > deadline:
                    raise DeadlineExceeded(
                        f"téléchargement trop lent (plus de {deadline:.0f} s) : {url}"
                    )
                size += len(chunk)
                if size > max_bytes:
                    raise error(
                        f"réponse trop volumineuse (plus de {max_bytes // 1024 // 1024} Mo) : {url}"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise error(str(exc)) from exc


def getter(
    *,
    timeout: float,
    max_bytes: int,
    error: type[Exception],
    headers: Callable[[], Mapping[str, str]] | None = None,
    remaining: Callable[[], float] | None = None,
) -> Getter:
    """Build a getter for one source.

    ``headers`` and ``remaining`` are callables, evaluated on each request
    (settings read at call time, time budget left).
    """

    def get(url: str) -> bytes:
        return fetch(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            error=error,
            headers=headers() if headers else None,
            deadline=remaining() if remaining else None,
        )

    return get


def as_list(node: object) -> list:
    """Wrap a single object in a list.

    These exports are converted from XML, so a one-element collection comes out
    as a bare object.
    """
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]
