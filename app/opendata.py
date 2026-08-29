"""Shared plumbing for the French open-data sources.

Four modules read government exports — the Assemblée nationale dossiers, the
Journal Officiel archives, the sitting records and the INSEE population
estimates. They had grown four near-identical copies of the same ``httpx.get``
wrapper, each with its own timeout and its own exception class, and two copies of
the same XML→JSON list normaliser.

Both live here now, so every source fetches bytes the same way and the injectable
getter used by the tests is genuinely one seam rather than four look-alikes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

#: Every source is fetched as bytes and parsed by its own module. Keeping the
#: return type identical across sources is what makes this a single seam.
Getter = Callable[[str], bytes]

#: Read in chunks this size while measuring against the cap below.
_CHUNK = 64 * 1024


class DeadlineExceeded(RuntimeError):
    """A caller's whole time budget ran out. Not the calling module's own error.

    That distinction is the point of having a separate class. Every source's
    error — ``LegifranceFetchError``, ``DebatesFetchError`` — means "this one
    document could not be read", and callers handle it by carrying on: the
    sitting-record loop catches it per sitting and moves to the next one, because
    a partial set of speeches is worth having.

    A spent clock is the opposite kind of failure. Every remaining request will
    fail the same way and instantly, so a loop that treats it as "this one is
    missing" churns through the rest of its work to arrive nowhere. Raising
    something the per-source handlers do not catch is what stops the loop at the
    first refusal, and it is why this lives here rather than beside the job that
    sets the budget: the module that spends the time owns the way of saying it
    ran out.
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
    """GET ``url``, raising ``error`` on any HTTP or network failure.

    ``error`` is the calling module's own exception class, so a failure still
    reads as "the Journal Officiel is unreachable" rather than as a generic
    transport error, and callers keep catching what they already caught.

    ``max_bytes`` is not optional and has no default, because the thing it
    prevents is the thing that used to happen: the body was read whole into
    memory with nothing bounding it. Both archive readers cap the *members* they
    unpack with care — see `app.legifrance.MAX_MEMBER_BYTES` and
    `app.assemblee.MAX_DOSSIER_BYTES` — and then handed the download itself no
    limit at all, so the only thing standing between a bad day upstream and the
    host was a container memory limit. A truncated read here is a caught error
    and a retry an hour later; an OOM is the site.

    The cap is checked as the body arrives rather than against
    ``Content-Length``, which is absent on a chunked response and is in any case
    a claim by the sender rather than a fact.

    ``deadline`` — seconds this request may take **in total** — bounds the one
    thing ``timeout`` does not. An httpx timeout is per socket operation, so a
    server dribbling one byte every few seconds resets it for ever and the read
    never ends; that is fine for the nightly import jobs, which have all night,
    and not fine for the draft, which has a budget it promises to keep. Left
    ``None`` nothing changes and the download is bounded only by ``max_bytes``.

    Given one, this narrows the socket timeout to it as well: no single operation
    should be allowed to outlast the whole request it belongs to.
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

    ``headers`` is a callable, not a mapping: INSEE's optional API key is read
    from the settings at call time, so binding it at import would freeze whatever
    the environment held when the module was first imported.

    ``remaining`` is a callable for a sharper reason: it is asked *per request*
    how much of the caller's budget is left, so successive fetches down one
    getter each get the time that is actually still there rather than the time
    there was when the getter was built.
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
    """Normalise an XML→JSON collection that may have collapsed to one object.

    The government exports are mechanical XML→JSON conversions, so a collection
    holding a single element is emitted as a bare object rather than as a
    one-element list. Every collection is read through this, and none is ever
    indexed directly.
    """
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]
