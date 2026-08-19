"""Fetching the official text of a promulgated law from the DILA JO archives."""

import io
import tarfile
from datetime import date

from app import legifrance
from app.config import get_settings
from app.legifrance import LegifranceFetchError, fetch_law_text

BASE = get_settings().jorf_dumps_url


def _jorf_xml(nor: str, num: str, nature: str = "LOI", articles: int = 2) -> bytes:
    """A JORFTEXT document in the shape the DILA archives publish."""
    body = "".join(
        f"""<ARTICLE><ID>JORFARTI{i}</ID><NUM>{i}</NUM>
        <BLOC_TEXTUEL><CONTENU><p>Contenu de l'article {i}.</p></CONTENU></BLOC_TEXTUEL>
        </ARTICLE>"""
        for i in range(1, articles + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TEXTE>
  <ID>JORFTEXT000000000001</ID>
  <ID_ELI>https://www.legifrance.gouv.fr/eli/loi/2026/7/27/{nor[:-1]}/jo/texte</ID_ELI>
  <ELI_ALIAS>
    <ID_ELI_ALIAS>https://www.legifrance.gouv.fr/eli/loi/2026/7/27/{num}/jo/texte</ID_ELI_ALIAS>
  </ELI_ALIAS>
  <NATURE>{nature}</NATURE>
  <NUM>{num}</NUM>
  <NOR>{nor}</NOR>
  <DATE_PUBLI>2026-07-28</DATE_PUBLI>
  <DATE_TEXTE>2026-07-27</DATE_TEXTE>
  <TITRE>LOI n°{num} du 27 juillet 2026</TITRE>
  <TITREFULL>LOI n° {num} du 27 juillet 2026 portant diverses dispositions</TITREFULL>
  <NOTICE><CONTENU><p>Notice explicative.</p></CONTENU></NOTICE>
  <STRUCT><SECTION_TA>{body}</SECTION_TA></STRUCT>
</TEXTE>""".encode()


def _tar(texts: dict[str, bytes]) -> bytes:
    """An archive laid out like a real one: texts plus versions.xml noise."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        # The real archives are ~97% versions.xml bookkeeping; the fetcher must
        # filter those out by name rather than parse them.
        for i in range(3):
            noise = b"<VERSIONS/>"
            info = tarfile.TarInfo(f"d/jorf/simple/JORF/CONT/00/{i}/versions.xml")
            info.size = len(noise)
            archive.addfile(info, io.BytesIO(noise))
        for name, data in texts.items():
            info = tarfile.TarInfo(f"d/jorf/simple/JORF/CONT/00/00/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _index(*names: str) -> bytes:
    """The directory listing, which names each archive twice (href + label)."""
    rows = "".join(f'<tr><td><a href="{n}">{n}</a></td><td>1.2M</td></tr>' for n in names)
    return f"<html><body><table>{rows}</table></body></html>".encode()


MORNING = "JORFSIMPLE_20260728-003542.tar.gz"
EVENING = "JORFSIMPLE_20260728-220005.tar.gz"
NEXT_DAY = "JORFSIMPLE_20260729-002201.tar.gz"
OUR_LAW = _jorf_xml("SFHX2609544L", "2026-668", articles=3)
OTHER_LAW = _jorf_xml("XXXX1234567L", "2026-999")


def _getter(archives: dict[str, bytes], index: bytes | None = None):
    """Route the index URL and each archive name, like the real server."""
    listing = index if index is not None else _index(*archives)

    def get(url: str) -> bytes:
        if url == BASE:
            return listing
        name = url.rsplit("/", 1)[-1]
        if name in archives:
            return archives[name]
        raise LegifranceFetchError(f"404 for {name}")

    return get


def test_fetches_the_law_by_nor():
    get = _getter({MORNING: _tar({"JORFTEXT01.xml": OTHER_LAW, "JORFTEXT02.xml": OUR_LAW})})
    law = fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)

    assert law.law_code == "2026-668"
    assert law.nature == "LOI"
    assert law.signed_on == date(2026, 7, 27)
    assert law.published_on == date(2026, 7, 28)
    assert law.title.startswith("LOI n° 2026-668")
    assert law.source_dump == MORNING
    assert [a.num for a in law.articles] == ["1", "2", "3"]
    assert law.articles[0].text == "Contenu de l'article 1."
    assert law.notice == "Notice explicative."
    # The number-based ELI alias is preferred over the NOR-based ID_ELI.
    assert law.eli_url.endswith("/2026-668/jo/texte")
    assert law.plain_text.startswith("LOI n° 2026-668")
    assert "Article 3. Contenu de l'article 3." in law.plain_text


def test_tries_archives_oldest_first_and_falls_through():
    """The morning archive is tried first; a miss falls through to the evening one."""
    get = _getter({
        MORNING: _tar({"JORFTEXT01.xml": OTHER_LAW}),
        EVENING: _tar({"JORFTEXT02.xml": OUR_LAW}),
    })
    law = fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)
    assert law.source_dump == EVENING


def test_falls_through_to_the_next_day():
    """A text published late can only appear in the following day's archive."""
    get = _getter({
        MORNING: _tar({"JORFTEXT01.xml": OTHER_LAW}),
        NEXT_DAY: _tar({"JORFTEXT02.xml": OUR_LAW}),
    })
    law = fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)
    assert law.source_dump == NEXT_DAY


def test_ignores_a_different_text_carrying_the_same_nor():
    """A rectificatif or decree with our NOR is not the law we asked for."""
    decree = _jorf_xml("SFHX2609544L", "2026-668", nature="DECRET")
    get = _getter({MORNING: _tar({"JORFTEXT01.xml": decree})})
    try:
        fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError as exc:
        assert "introuvable" in str(exc)


def test_raises_when_the_law_is_absent():
    get = _getter({MORNING: _tar({"JORFTEXT01.xml": OTHER_LAW})})
    try:
        fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError:
        pass


def test_raises_when_no_archive_covers_the_date():
    """Only ~13 months of daily archives are retained — say so plainly."""
    get = _getter({MORNING: _tar({"JORFTEXT01.xml": OUR_LAW})})
    try:
        fetch_law_text("SFHX2609544L", date(2019, 1, 1), get=get)
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError as exc:
        assert "treize mois" in str(exc)


def test_raises_on_network_error():
    def boom(url):
        raise LegifranceFetchError("network down")

    try:
        fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=boom)
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError:
        pass


def test_raises_on_corrupt_archive():
    get = _getter({MORNING: b"not a tar.gz at all"})
    try:
        fetch_law_text("SFHX2609544L", date(2026, 7, 28), get=get)
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError as exc:
        assert "illisible" in str(exc)


def test_raises_on_missing_nor():
    try:
        fetch_law_text("", date(2026, 7, 28), get=_getter({}))
        raise AssertionError("expected LegifranceFetchError")
    except LegifranceFetchError:
        pass


def test_list_dumps_dedupes_and_orders():
    """The real index names each archive twice; ordering must stay chronological."""
    get = _getter({}, index=_index(EVENING, EVENING, MORNING, MORNING, NEXT_DAY))
    dumps = legifrance.list_dumps(get)
    assert dumps["20260728"] == [MORNING, EVENING]
    assert dumps["20260729"] == [NEXT_DAY]
