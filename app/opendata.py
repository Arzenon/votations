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

from collections.abc import Callable, Mapping

import httpx

#: Every source is fetched as bytes and parsed by its own module. Keeping the
#: return type identical across sources is what makes this a single seam.
Getter = Callable[[str], bytes]

#: Read in chunks this size while measuring against the cap below.
_CHUNK = 64 * 1024


def fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    error: type[Exception],
    headers: Mapping[str, str] | None = None,
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
    """
    try:
        with httpx.stream(
            "GET", url, timeout=timeout, follow_redirects=True, headers=headers
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes(_CHUNK):
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
) -> Getter:
    """Build the default getter for one source.

    ``headers`` is a callable, not a mapping: INSEE's optional API key is read
    from the settings at call time, so binding it at import would freeze whatever
    the environment held when the module was first imported.
    """

    def get(url: str) -> bytes:
        return fetch(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            error=error,
            headers=headers() if headers else None,
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
