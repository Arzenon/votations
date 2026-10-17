"""Fetch a law's official text from the DILA open data.

The Journal Officiel is published as twice-daily archives on
``echanges.dila.gouv.fr``. A law is found by its NOR in the archive for its
publication date. Nothing is stored: a law can be 230,000 characters and the
text is always one request away. A law usually lands in the morning archive of
its JO date, sometimes in the evening one, so candidates are tried in order.
"""

from __future__ import annotations

import io
import re
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from xml.etree.ElementTree import Element  # type only, parsing uses defusedxml

from defusedxml import ElementTree as ET
from defusedxml.ElementTree import ParseError

from app.config import get_settings
from app.opendata import Getter, getter

# Archive names look like `JORFSIMPLE_20260818-223511.tar.gz`.
DUMP_RE = re.compile(r"JORFSIMPLE_(\d{8})-(\d{6})\.tar\.gz")

# About 14,000 XML files per archive, only a few hundred are texts. Filter by
# name before parsing.
TEXT_MEMBER_RE = re.compile(r"/JORFTEXT\d+\.xml$")

# How many daily archives to try before giving up (jo_date, then the day after).
MAX_CANDIDATES = 6

# Max size of one JO text. The largest (lois de finances) are a few hundred kB.
MAX_MEMBER_BYTES = 16 * 1024 * 1024


class LegifranceFetchError(RuntimeError):
    """Network, archive or parse failure, or law not found."""


@dataclass
class Article:
    num: str
    text: str


@dataclass
class LawText:
    """The official text of one law, as published at the Journal Officiel."""

    nor: str
    law_code: str  # NUM, e.g. "2026-791"
    title: str  # TITREFULL
    nature: str  # NATURE, e.g. "LOI"
    signed_on: date | None  # DATE_TEXTE, the promulgation date
    eli_url: str | None  # canonical Légifrance (ELI) URL
    notice: str = ""  # plain-language notice, when the JO carries one
    articles: list[Article] = field(default_factory=list)
    source_dump: str = ""  # which archive it came from

    @property
    def plain_text(self) -> str:
        """The whole law as plain text."""
        body = "\n\n".join(f"Article {a.num}. {a.text}".strip() for a in self.articles)
        return f"{self.title}\n\n{body}".strip()


# Archives are 200 kB to 8 MB. Drafts run in the app container (1 GB, several
# at once) and use about 3x this in memory, so keep it modest.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024

# Per socket operation, not per download (see `bounded_getter`).
TIMEOUT = 180.0

_default_getter = getter(timeout=TIMEOUT, max_bytes=MAX_ARCHIVE_BYTES, error=LegifranceFetchError)


def bounded_getter(remaining: Callable[[], float]) -> Getter:
    """A getter limited by the caller's time budget (for the drafting job).

    DeadlineExceeded isn't caught by `fetch_law_text`, so the search stops
    instead of reporting the law as missing.
    """
    return getter(
        timeout=TIMEOUT,
        max_bytes=MAX_ARCHIVE_BYTES,
        error=LegifranceFetchError,
        remaining=remaining,
    )


def _base_url() -> str:
    base = get_settings().jorf_dumps_url
    return base if base.endswith("/") else base + "/"


def list_dumps(get: Getter | None = None) -> dict[str, list[str]]:
    """Map ``YYYYMMDD`` to archive names, oldest first."""
    get = get or _default_getter
    index = get(_base_url()).decode("utf-8", "replace")
    dumps: dict[str, list[str]] = {}
    for name, day, _time in ((m.group(0), m.group(1), m.group(2)) for m in DUMP_RE.finditer(index)):
        bucket = dumps.setdefault(day, [])
        if name not in bucket:  # the index lists each file twice (href + label)
            bucket.append(name)
    for bucket in dumps.values():
        bucket.sort()
    return dumps


def _candidates(dumps: dict[str, list[str]], jo_date: date) -> list[str]:
    """Archives that may carry a text published on ``jo_date``, likeliest first."""
    names: list[str] = []
    for offset in (0, 1):
        names.extend(dumps.get((jo_date + timedelta(days=offset)).strftime("%Y%m%d"), []))
    return names[:MAX_CANDIDATES]


def _text(node: Element | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_law(data: bytes, nor: str, source: str) -> LawText | None:
    """Build a LawText from one JORFTEXT document, or None if it isn't our law."""
    try:
        # defusedxml protects against entity expansion attacks.
        root = ET.fromstring(data)
    except ParseError:
        return None
    if (root.findtext("NOR") or "").strip().upper() != nor.strip().upper():
        return None
    # Ordinary and organic laws are both "LOI"; skip rectificatifs, decrees, etc.
    nature = (root.findtext("NATURE") or "").strip()
    if nature.upper() != "LOI":
        return None

    articles = []
    for node in root.findall(".//ARTICLE"):
        body = _text(node.find("BLOC_TEXTUEL"))
        if body:
            articles.append(Article(num=(node.findtext("NUM") or "?").strip(), text=body))

    # ID_ELI is NOR-based; the alias uses the law number and reads better.
    alias = root.findtext("ELI_ALIAS/ID_ELI_ALIAS")
    return LawText(
        nor=(root.findtext("NOR") or "").strip(),
        law_code=(root.findtext("NUM") or "").strip(),
        title=(root.findtext("TITREFULL") or root.findtext("TITRE") or "").strip(),
        nature=nature,
        signed_on=_parse_date(root.findtext("DATE_TEXTE")),
        eli_url=(alias or root.findtext("ID_ELI") or "").strip() or None,
        notice=_text(root.find("NOTICE")),
        articles=articles,
        source_dump=source,
    )


def _search_archive(blob: bytes, nor: str, source: str) -> LawText | None:
    needle = nor.strip().upper().encode()
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            for member in archive:
                if not (member.isfile() and TEXT_MEMBER_RE.search(member.name)):
                    continue
                if member.size > MAX_MEMBER_BYTES:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                if needle not in data.upper():
                    continue
                law = _parse_law(data, nor, source)
                if law is not None:
                    return law
    except (tarfile.TarError, EOFError) as exc:
        raise LegifranceFetchError(f"archive illisible : {exc}") from exc
    return None


def fetch_law_text(nor: str, jo_date: date, get: Getter | None = None) -> LawText:
    """Fetch the official text of the law with this NOR, published on ``jo_date``.

    Raises LegifranceFetchError on network failure or if the law is not found in
    any archive covering that date.
    """
    if not nor:
        raise LegifranceFetchError("NOR manquant")
    get = get or _default_getter

    dumps = list_dumps(get)
    candidates = _candidates(dumps, jo_date)
    if not candidates:
        raise LegifranceFetchError(
            f"aucune archive JO pour le {jo_date} "
            "(les archives quotidiennes ne remontent qu'à environ treize mois)"
        )

    base = _base_url()
    for name in candidates:
        law = _search_archive(get(base + name), nor, name)
        if law is not None:
            return law
    raise LegifranceFetchError(
        f"texte introuvable pour le NOR {nor} (archives essayées : {', '.join(candidates)})"
    )
