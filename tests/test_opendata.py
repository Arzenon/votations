"""The shared fetcher every open-data source goes through.

Four modules read government exports, and all four fetch bytes here. The parsing
each of them does is tested against injected fixtures elsewhere; what is tested
here is the one thing those tests cannot see, because they never reach the
network: how much of a response this is willing to read.
"""

from __future__ import annotations

import httpx
import pytest

from app.assemblee import MAX_EXPORT_BYTES, AssembleeFetchError
from app.debates import MAX_RECORD_BYTES, DebatesFetchError
from app.insee import MAX_PAYLOAD_BYTES, InseeFetchError
from app.legifrance import MAX_ARCHIVE_BYTES, LegifranceFetchError
from app.opendata import fetch, getter


class _Boom(RuntimeError):
    """Stands in for a source module's own exception class."""


def _served(monkeypatch, response: httpx.Response) -> None:
    """Answer every request with this response, through httpx's own transport."""

    def stream(method, url, **kwargs):
        kwargs.pop("follow_redirects", None)
        client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
        return client.stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "stream", stream)


def test_a_body_within_the_cap_comes_back_whole(monkeypatch):
    _served(monkeypatch, httpx.Response(200, content=b"x" * 5000))
    assert fetch("https://example.test/f", timeout=5, max_bytes=10_000, error=_Boom) == b"x" * 5000


def test_an_oversized_body_is_refused_rather_than_read(monkeypatch):
    """The failure this prevents is an OOM, not a parse error.

    Both archive readers cap the members they unpack and then handed the download
    itself no limit at all, so the only thing between a bad day upstream and the
    host was a container memory limit.
    """
    _served(monkeypatch, httpx.Response(200, content=b"x" * 300_000))
    with pytest.raises(_Boom, match="trop volumineuse"):
        fetch("https://example.test/f", timeout=5, max_bytes=100_000, error=_Boom)


def test_the_cap_is_measured_not_believed(monkeypatch):
    """A `Content-Length` is a claim by the sender; the bytes are the fact."""
    _served(
        monkeypatch,
        httpx.Response(200, content=b"x" * 300_000, headers={"content-length": "10"}),
    )
    with pytest.raises(_Boom, match="trop volumineuse"):
        fetch("https://example.test/f", timeout=5, max_bytes=100_000, error=_Boom)


def test_an_http_failure_still_reads_as_the_source_being_unreachable(monkeypatch):
    _served(monkeypatch, httpx.Response(503))
    with pytest.raises(_Boom):
        fetch("https://example.test/f", timeout=5, max_bytes=100_000, error=_Boom)


def test_a_built_getter_carries_the_cap(monkeypatch):
    _served(monkeypatch, httpx.Response(200, content=b"x" * 300_000))
    get = getter(timeout=5, max_bytes=1000, error=_Boom)
    with pytest.raises(_Boom, match="trop volumineuse"):
        get("https://example.test/f")


def test_every_source_declares_a_ceiling():
    """A source added without one would be the old behaviour, quietly restored.

    `fetch` has no default for `max_bytes` precisely so that cannot happen by
    omission; this pins that each source has in fact chosen a number, and that
    each number is in the right order of magnitude for what that source serves.
    """
    ceilings = {
        # (module error class, ceiling, what the source actually sends)
        LegifranceFetchError: (MAX_ARCHIVE_BYTES, 8 * 1024 * 1024),
        AssembleeFetchError: (MAX_EXPORT_BYTES, 10 * 1024 * 1024),
        DebatesFetchError: (MAX_RECORD_BYTES, 1024 * 1024),
        InseeFetchError: (MAX_PAYLOAD_BYTES, 1024 * 1024),
    }
    for error, (ceiling, typical) in ceilings.items():
        assert ceiling > typical, f"{error.__name__}: ceiling below a normal response"
        assert ceiling <= 256 * 1024 * 1024, f"{error.__name__}: ceiling too high to bound anything"
