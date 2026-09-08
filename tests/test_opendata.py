"""The shared fetcher every open-data source goes through.

Four modules read government exports, and all four fetch bytes here. The parsing
each of them does is tested against injected fixtures elsewhere; what is tested
here is the one thing those tests cannot see, because they never reach the
network: how much of a response this is willing to read.
"""

from __future__ import annotations

import io
import json
import zipfile

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


# --- Choisir le bon scrutin ----------------------------------------------------


def _scrutin(uid, sort="adopté", objet="l'ensemble du projet de loi.", pour=300, contre=100):
    from app.scrutins import Scrutin

    return Scrutin(uid=uid, sort=sort, objet=objet, pour=pour, contre=contre, abstentions=5)


def test_the_last_adoption_is_the_one_that_settles_the_law():
    """A law read three times has three recorded votes; only the final one counts."""
    from app.scrutins import final_vote

    index = {
        "a": _scrutin("a", pour=100, contre=200),
        "b": _scrutin("b", pour=357, contre=177),
    }
    assert final_vote(["a", "b"], index).pour == 357


def test_a_vote_that_did_not_adopt_the_text_is_ignored():
    from app.scrutins import final_vote

    index = {"a": _scrutin("a"), "b": _scrutin("b", sort="rejeté")}
    chosen = final_vote(["a", "b"], index)
    assert chosen is not None and chosen.uid == "a", "le rejet ultérieur ne remplace pas"


def test_a_vote_on_part_of_a_text_is_not_a_vote_on_the_text():
    """Three lois de finances carry one, and one of them reads « l'ensemble de… ».

    The Assemblée rejected « la première partie » in first reading and the law was
    adopted later in commission mixte paritaire, so a check for « l'ensemble »
    passes it straight through.
    """
    from app.scrutins import final_vote

    partielle = _scrutin("p", objet="l'ensemble de la première partie du projet de loi de finances")
    assert final_vote(["p"], {"p": partielle}) is None


def test_a_law_with_no_recorded_vote_resolves_to_nothing():
    from app.scrutins import final_vote

    assert final_vote([], {}) is None
    assert final_vote(["inconnu"], {}) is None


# --- Lire l'archive des scrutins ----------------------------------------------


def _scrutins_zip(entries: dict[str, str]) -> bytes:
    """An in-memory copy of the scrutins export."""
    from app.scrutins import MEMBER

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for uid, body in entries.items():
            archive.writestr(MEMBER.format(uid=uid), body)
    return buf.getvalue()


def test_a_scrutin_is_read_out_of_the_archive_by_name():
    from app.scrutins import fetch_votes

    body = json.dumps(
        {
            "scrutin": {
                "uid": "VTANR5L17V1234",
                "sort": {"code": "adopté"},
                "objet": {"libelle": "l'ensemble du projet de loi."},
                "syntheseVote": {"decompte": {"pour": "357", "contre": "177", "abstentions": "9"}},
            }
        }
    )
    blob = _scrutins_zip({"VTANR5L17V1234": body})
    found = fetch_votes({"VTANR5L17V1234"}, get=lambda url: blob)
    assert found["VTANR5L17V1234"].pour == 357
    assert found["VTANR5L17V1234"].contre == 177


def test_a_scrutin_larger_than_the_cap_is_not_read():
    """The same rule the dossiers and JO readers apply, in the third reader.

    A zip member declares its own uncompressed size, and the download cap bounds
    the *compressed* bytes only — so reading a member without looking first is how
    a few kilobytes of archive become gigabytes of memory. This reader was the one
    that did not look.
    """
    from app.scrutins import MAX_MEMBER_BYTES, fetch_votes

    blob = _scrutins_zip({"gros": "p" * (MAX_MEMBER_BYTES + 10)})
    assert fetch_votes({"gros"}, get=lambda url: blob) == {}


def test_one_unreadable_scrutin_does_not_lose_the_others():
    from app.scrutins import fetch_votes

    good = json.dumps(
        {
            "scrutin": {
                "uid": "bon",
                "sort": {"code": "adopté"},
                "syntheseVote": {"decompte": {"pour": "10", "contre": "2", "abstentions": "0"}},
            }
        }
    )
    blob = _scrutins_zip({"bon": good, "casse": "{ pas du json"})
    found = fetch_votes({"bon", "casse"}, get=lambda url: blob)
    assert set(found) == {"bon"}
