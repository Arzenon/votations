"""Fetch the official text of a promulgated law from the DILA open data.

The Journal Officiel is published as twice-daily incremental archives on
``echanges.dila.gouv.fr`` (Licence Ouverte, no authentication). A law is located
by its **NOR**, the reference the Assemblée nationale import already stores, in
the archive covering its publication date.

Design notes:
- The HTTP getter is injectable so the parsing is unit-testable offline.
- Any failure raises :class:`LegifranceFetchError`; nothing is cached or stored.
- Text is fetched on demand, never persisted: a single law can run to 230 000
  characters, and the official text is always one request away.
- A law published on day D usually lands in D's early-morning archive (a few
  hundred kB) but sometimes only in the evening one, so candidates are tried
  oldest-first and the search stops at the first hit.
"""

from __future__ import annotations

import io
import re
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from xml.etree.ElementTree import Element  # type only — parsing goes through defusedxml

import httpx
from defusedxml import ElementTree as ET
from defusedxml.ElementTree import ParseError

from app.config import get_settings

# Archive names look like `JORFSIMPLE_20260818-223511.tar.gz`.
DUMP_RE = re.compile(r"JORFSIMPLE_(\d{8})-(\d{6})\.tar\.gz")

# Each archive holds ~14 000 XML files, of which only a few hundred are the
# texts themselves (the rest are `versions.xml` bookkeeping) — filter by name
# before parsing anything.
TEXT_MEMBER_RE = re.compile(r"/JORFTEXT\d+\.xml$")

# How many daily archives to try before giving up (jo_date, then the day after).
MAX_CANDIDATES = 6

# The largest single JO text worth reading. The biggest real ones (a loi de
# finances) run to a few hundred kilobytes; anything past this is a malformed or
# hostile archive member and is skipped rather than read into memory.
MAX_MEMBER_BYTES = 16 * 1024 * 1024


class LegifranceFetchError(RuntimeError):
    """Raised on any network/archive/parse problem, or when the law is absent."""


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
    signed_on: date | None  # DATE_TEXTE — the promulgation date
    published_on: date | None  # DATE_PUBLI — the JO date
    eli_url: str | None  # canonical Légifrance (ELI) URL
    notice: str = ""  # plain-language notice, when the JO carries one
    articles: list[Article] = field(default_factory=list)
    source_dump: str = ""  # provenance: which archive it came from

    @property
    def plain_text(self) -> str:
        """The whole law as plain text, ready to hand to a reader or a model."""
        body = "\n\n".join(f"Article {a.num}. {a.text}".strip() for a in self.articles)
        return f"{self.title}\n\n{body}".strip()


Getter = Callable[[str], bytes]


def _default_getter(url: str) -> bytes:
    # Archives run from ~200 kB to ~8 MB; the index itself is a small HTML page.
    try:
        resp = httpx.get(url, timeout=180.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as exc:
        raise LegifranceFetchError(str(exc)) from exc


def _base_url() -> str:
    base = get_settings().jorf_dumps_url
    return base if base.endswith("/") else base + "/"


def list_dumps(get: Getter | None = None) -> dict[str, list[str]]:
    """Map ``YYYYMMDD`` → archive names, each list ordered oldest-first."""
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
        # defusedxml: the archives are fetched over the network, and a few
        # kilobytes of nested entities would otherwise expand to gigabytes.
        root = ET.fromstring(data)
    except ParseError:
        return None
    if (root.findtext("NOR") or "").strip().upper() != nor.strip().upper():
        return None
    # Ordinary and organic laws both carry NATURE "LOI"; anything else with this
    # NOR (a rectificatif, a decree) is not the text we were asked for.
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
        published_on=_parse_date(root.findtext("DATE_PUBLI")),
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
                    continue  # no single JO text is this large; skip rather than read
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
