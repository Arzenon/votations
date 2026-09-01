"""CRUD and queries for laws."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime

from sqlalchemy import (
    ColumnClause,
    Select,
    and_,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, with_expression

from app.crud import Page, count_matching, paginate
from app.db import utcnow
from app.labels import french_date, french_datetime
from app.models import (
    SEARCH_VECTOR_QUERY_SQL,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_SCHEDULED,
    Law,
    LawRevision,
)

# Columns whose change is recorded once a law is published. Everything an
# administrator can edit is here: the reader's question is "was this text the one
# I voted on", and any of these changes the answer.
TRACKED_FIELDS = (
    "title",
    "official_ref",
    "legifrance_url",
    "summary_text",
    "practical_changes",
    "arguments_for",
    "arguments_against",
    "debate_summary",
    "promulgated_on",
    "opens_at",
    "closes_at",
    "published",
)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)[:255] or "loi"


#: Room kept for the ``-nn`` a collision appends, inside `laws.slug`'s width.
_SLUG_SUFFIX_ROOM = 8


def unique_slug(db: Session, title: str) -> str:
    """The first free slug derived from ``title``.

    One query rather than one per candidate: the loop used to ask the database
    "is `loi-x` taken? `loi-x-2`? `loi-x-3`?" one round trip at a time, so a
    title shared by n laws cost n of them. Reading the whole family at once and
    choosing in Python costs one, whatever n is.

    The base is trimmed so the suffix fits: `slugify` already cuts at the column's
    255 characters, which left no room for the `-2` and turned a collision between
    two very long titles into a value the column could not hold.
    """
    base = slugify(title)[: 255 - _SLUG_SUFFIX_ROOM]
    taken = set(
        db.scalars(
            select(Law.slug).where(
                or_(Law.slug == base, Law.slug.like(f"{_escape_like(base)}-%", escape="\\"))
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


#: Deferred on every list view. These are the expensive part of a law — the
#: practical-changes prose and the two JSON argument lists — and only the law
#: page and the admin form render them, both of which load a single law by key.
#: Fetching them to draw a table of titles and dates was most of the bytes on
#: every listing. Deferred rather than dropped: an attribute touched anyway still
#: works, it just costs a query, so this can never turn a page into a 500.
_LIST_DEFERRED = (
    defer(Law.practical_changes),
    defer(Law.arguments_for),
    defer(Law.arguments_against),
)

#: For every listing: none of them renders a whole summary. The search results
#: and the administration table show only titles, references and dates; the home
#: page shows an excerpt, which it gets as :data:`_EXCERPT` below rather than by
#: reading the column and throwing most of it away.
_TITLES_ONLY_DEFERRED = (*_LIST_DEFERRED, defer(Law.summary_text))

#: How much of a summary the home page shows before trailing off.
EXCERPT_CHARS = 200

#: The excerpt, cut by the database. One character more than is displayed, which
#: is exactly what the template needs to know whether to add an ellipsis — and
#: the whole reason this is a `left()` and not a slice in Python.
_EXCERPT = with_expression(Law.excerpt, func.left(Law.summary_text, EXCERPT_CHARS + 1))


def get_by_slug(db: Session, slug: str) -> Law | None:
    return db.scalar(select(Law).where(Law.slug == slug))


def get_by_id(db: Session, law_id: int) -> Law | None:
    return db.get(Law, law_id)


def _published(stmt: Select) -> Select:
    """Restrict to what the public may see: published, and not withdrawn.

    **The bare column, not ``.is_(True)``, and that is not a style preference.**
    Postgres decides whether a partial index applies by *proving* the query's
    WHERE implies the index's predicate, and it cannot prove that ``published IS
    true`` implies ``published``. So the explicit form silently disqualified
    `ix_laws_home` — the index built for exactly this clause — and every public
    listing went back to reading the whole table and sorting it: 26.8 ms against
    0.3 ms on the home page's « ouvertes » band at 50 000 laws.

    The two spellings are identical SQL as far as results go, which is what makes
    this worth a comment: tidying it back would cost a hundredfold and change
    nothing anybody could see until the corpus was large.
    """
    return stmt.where(Law.published, Law.archived_at.is_(None))


#: How the public listings are ordered, and the only place that decision is made.
#:
#: By the day Parliament acted, most recent first. Not by `created_at`, which is
#: when an administrator pressed save — three laws entered in one evening ranked
#: in the order they were typed. Not by the closing date either: at launch every
#: votation runs for a year, so closing dates are all within days of each other
#: and sorting on them collapses straight back into data-entry order.
#:
#: NULLS LAST is load-bearing. Postgres puts nulls *first* in a descending sort,
#: so a law written by hand — which has no staged row and therefore no date —
#: would otherwise open the site. `created_at` breaks the tie beneath, so the
#: undated ones keep a stable order among themselves instead of shuffling.
_NEWEST_LAW_FIRST = (Law.promulgated_on.desc().nullslast(), Law.created_at.desc())


def list_published(
    db: Session, *, status: str | None = None, limit: int = 50, now: datetime | None = None
) -> list[Law]:
    """Published, non-archived laws, most recently promulgated first (home page).

    Carries :attr:`~app.models.law.Law.excerpt`, not the whole summary.

    ``status`` narrows to one of the derived states, in SQL — see
    :func:`_apply_status`. That is what lets :func:`published_by_state` apply the
    limit to each band rather than to the page as a whole, and ``now`` is what
    keeps the three bands it asks for consistent with each other.
    """
    stmt = (
        _apply_status(_published(select(Law)), status, now=now)
        .options(*_TITLES_ONLY_DEFERRED, _EXCERPT)
        .order_by(*_NEWEST_LAW_FIRST)
        .limit(limit)
    )
    return list(db.scalars(stmt))


#: The bands the home page shows, in the order a visitor needs them: what you can
#: vote on, what is coming, what is decided.
HOME_BANDS = (STATUS_OPEN, STATUS_SCHEDULED, STATUS_CLOSED)


def published_by_state(db: Session, *, limit: int = 50) -> list[tuple[str, list[Law]]]:
    """The home page's listing, grouped by what a visitor can do with each law.

    Empty bands are dropped, and that is what makes this safe to ship before it
    is needed: while every votation is open — which is what the first months look
    like, with a year-long voting window — exactly one band survives and the page
    renders as a plain chronological list. It starts grouping itself on the day
    the windows diverge, with no change here and none to the ordering.

    **``limit`` is per band, and has to be.** This used to take the ``limit``
    most recently promulgated laws and group them afterwards, in one query. That
    reads as an optimisation and is a bug waiting for the corpus to grow: once
    more than ``limit`` laws are published, an open votation whose promulgation
    date is older than the newest fifty falls off the page — out of « Votations
    ouvertes », the one band a visitor has something to *do* with — while fifty
    closed ones fill it. The whole point of the bands is that what you can vote
    on cannot be crowded out by what you cannot.

    So it is one query per band. Three indexed reads of a table holding hundreds
    of rows, against a correctness bug that only shows up in production and only
    once it is too late to notice.

    All three read the clock at the *same* instant, and that is not decoration.
    The states are derived from the voting window, so three queries asking three
    slightly different "now"s can disagree about one law: a votation closing in
    the microseconds between the first query and the third is open to one and
    closed to the other, and appears on the page twice. Grouping in Python could
    not do that, because it classified each row once — pinning the instant is
    what buys the same guarantee back.
    """
    now = utcnow()
    bands = [
        (state, list_published(db, status=state, limit=limit, now=now)) for state in HOME_BANDS
    ]
    return [(state, laws) for state, laws in bands if laws]


def public_urls(db: Session, *, limit: int = 5000) -> list[tuple[str, datetime]]:
    """(slug, last-modified) for every publicly visible law — the sitemap.

    Deliberately not :func:`list_published`: a sitemap needs two values per law
    and used to load five thousand whole laws, prose and argument lists
    included, to read them.
    """
    rows = db.execute(
        _published(select(Law.slug, Law.updated_at, Law.created_at))
        .order_by(Law.created_at.desc())
        .limit(limit)
    ).all()
    return [(slug, updated or created) for slug, updated, created in rows]


def search(
    db: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> Page[Law]:
    """Advanced search (V1): across title/ref/summary, with status filter + pagination.

    Both the filter and the page window are applied in SQL, so a large corpus is
    never loaded into the process to be sliced afterwards.

    Ordered like the home page, by :data:`_NEWEST_LAW_FIRST`. It used to order by
    `created_at` — the moment an administrator pressed save — which is the very
    thing the promulgation date was added to stop the home page doing. Leaving
    search on it meant the same corpus was ranked two different ways depending on
    which page you reached it from, and only one of them was about the law.
    """
    stmt = _published(select(Law))
    if query:
        stmt = _apply_search(stmt, query)
    stmt = _apply_status(stmt, status)

    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.options(*_TITLES_ONLY_DEFERRED)
            .order_by(*_NEWEST_LAW_FIRST)
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards typed by a visitor, so `%` searches for a `%`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Characters that mean something to `to_tsquery` and nothing to a visitor.
_TSQUERY_NOISE = re.compile(r"[^\w\s-]", flags=re.UNICODE)


def _tsquery(query: str) -> str:
    """Turn what someone typed into a French tsquery, last word as a prefix.

    ``réforme des retraites`` becomes ``réforme & des & retraite:*`` — every word
    required, the last one open-ended so the results narrow as you type rather
    than vanishing on the way to a complete word. Stemming is what makes
    « retraite » find « retraites », and the French dictionary drops the
    stop-words on its own, so « des » costs nothing.

    Punctuation is stripped rather than escaped: a visitor typing an apostrophe
    or a colon means it as text, and `to_tsquery` would read it as syntax and
    fail the whole query.
    """
    words = _TSQUERY_NOISE.sub(" ", query).split()
    if not words:
        return ""
    *rest, last = words
    return " & ".join([*rest, f"{last}:*"])


#: The columns the full-text index covers. `official_ref` is deliberately absent:
#: « LOI n° 2026-214 » is looked up as a string, not as prose, so it keeps a plain
#: substring match alongside.
#:
#: The same expression the index is built on — defined beside the table, so the
#: two cannot drift and quietly leave every search on a sequential scan.
_SEARCH_VECTOR: ColumnClause[str] = literal_column(SEARCH_VECTOR_QUERY_SQL)


def _apply_search(stmt: Select, query: str) -> Select:
    """Full-text over the prose, substring over the reference.

    Two mechanisms because there are two kinds of search: people look for a
    *subject* in the title and summary, where stemming helps, and for a *number*
    in the official reference, where it would only get in the way.
    """
    like = f"%{_escape_like(query)}%"
    reference = Law.official_ref.ilike(like, escape="\\")

    terms = _tsquery(query)
    if not terms:
        return stmt.where(reference)
    return stmt.where(
        or_(
            _SEARCH_VECTOR.op("@@")(func.to_tsquery(literal("french", type_=REGCONFIG), terms)),
            reference,
        )
    )


def _apply_status(stmt: Select, status: str | None, *, now: datetime | None = None) -> Select:
    """Translate the derived status into window predicates the database can filter on.

    ``Law.status()`` computes the same thing from the live clock; expressing it in
    SQL is what lets the page window be applied before rows are fetched. Only the
    three states a published law can be in are handled — the caller has already
    required ``published``, so "draft" cannot match anything.

    ``now`` is for a caller asking about more than one state: the three states
    partition the laws only if all three are decided against the same instant.
    Left unset it reads the clock, which is right for the one-state callers.
    """
    if not status:
        return stmt
    now = now or utcnow()
    opened = or_(Law.opens_at.is_(None), Law.opens_at <= now)
    not_closed = or_(Law.closes_at.is_(None), Law.closes_at > now)
    if status == STATUS_OPEN:
        return stmt.where(and_(opened, not_closed))
    if status == STATUS_SCHEDULED:
        return stmt.where(Law.opens_at.is_not(None), Law.opens_at > now)
    if status == STATUS_CLOSED:
        return stmt.where(Law.closes_at.is_not(None), Law.closes_at <= now)
    return stmt


#: How many slugs :func:`create` will try before giving up. Each pass re-reads
#: the table, so the loop only turns while another transaction is committing the
#: exact slug this one just picked — a handful of attempts is already generous.
_SLUG_ATTEMPTS = 5


def create(db: Session, **fields) -> Law:
    """Insert a law, deriving a unique slug from its title when none is given.

    :func:`unique_slug` reads the slugs that exist and returns the first free
    one, which two simultaneous creations can answer identically: the read is not
    part of the insert. ``laws.slug`` is unique, so one of them used to fail with
    an IntegrityError and a 500 page. Retrying is the honest repair — on the next
    pass the loser can see the slug the winner committed and moves on to the next
    suffix.

    An explicitly supplied slug is not retried: colliding with an existing law is
    then the caller's own mistake, and re-deriving would produce the collision
    again.
    """
    explicit = bool(fields.get("slug"))
    attempt = 0
    while True:
        attempt += 1
        if not explicit:
            fields["slug"] = unique_slug(db, fields.get("title", ""))
        law = Law(**fields)
        db.add(law)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Past this many losses something other than a race is wrong, and
            # spinning would only hide it.
            if explicit or attempt >= _SLUG_ATTEMPTS:
                raise
            continue
        db.refresh(law)
        return law


def _as_text(value) -> str:
    """Render a column value for the revision record."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, datetime):
        # Readable, not machine-readable: this ends up on a public page.
        return french_datetime(value)
    if isinstance(value, date):
        # Checked after `datetime`, which subclasses `date`: a promulgation is a
        # day, and giving it a time would be inventing one.
        return french_date(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def update(db: Session, law: Law, **fields) -> Law:
    """Apply an edit, recording what changed if the law was already published.

    The record is what makes the transparency claim checkable: a law whose text
    moves while people vote on it would otherwise leave a published result
    describing something nobody read. Draft edits are not recorded — nobody has
    voted on a draft, and logging the ordinary act of writing the presentation
    would bury the changes that matter.
    """
    record = law.published
    changed_at = utcnow()
    for key, value in fields.items():
        before = getattr(law, key, None)
        if before == value:
            continue
        if record and key in TRACKED_FIELDS:
            db.add(
                LawRevision(
                    law_id=law.id,
                    changed_at=changed_at,
                    field=key,
                    old_value=_as_text(before),
                    new_value=_as_text(value),
                )
            )
        setattr(law, key, value)
    db.commit()
    db.refresh(law)
    return law


#: Revisions per page. Each row carries the whole before and after value of a
#: field, so twenty of them is already a long page — a summary rewritten twice is
#: four full presentation blocks of text.
REVISIONS_PAGE_SIZE = 20


def revisions(
    db: Session, law_id: int, *, page: int = 1, page_size: int = REVISIONS_PAGE_SIZE
) -> Page[LawRevision]:
    """Recorded changes to this law, newest first.

    Paginated because this is rendered on a *public* page and the underlying
    table has no ceiling: every edit to a published law adds a row holding both
    the old and the new value of a field, and those values are whole presentation
    blocks. A law corrected a few hundred times would otherwise have shipped all
    of it in one table, to every reader of its results.
    """
    stmt = select(LawRevision).where(LawRevision.law_id == law_id)
    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.order_by(LawRevision.changed_at.desc(), LawRevision.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def count_revisions(db: Session, law_id: int) -> int:
    return (
        db.scalar(select(func.count()).select_from(LawRevision).where(LawRevision.law_id == law_id))
        or 0
    )


def archive(db: Session, law: Law) -> Law:
    """Withdraw a law from the site without destroying anything.

    The votes stay. This is reversible, which is the whole point: deleting used to
    cascade away published open data behind a single click.

    Archiving twice does nothing the second time. The route answers a repeated
    POST with the same redirect rather than an error, precisely because the
    operation is idempotent — and it was not: the second call moved
    ``archived_at`` to the moment of the resubmission, so a browser's « back then
    submit again » silently rewrote the date the archive page displays.
    """
    if law.archived_at is None:
        law.archived_at = utcnow()
        db.commit()
        db.refresh(law)
    return law


def restore(db: Session, law: Law) -> Law:
    law.archived_at = None
    db.commit()
    db.refresh(law)
    return law


def purge(db: Session, law: Law) -> None:
    """Delete a law and every vote on it, for good.

    Deliberately separate from :func:`archive` and reachable only for a law that
    is already archived: reclaiming the disk an old votation occupies is a real
    operational need, but it must be a decision someone took twice.
    """
    db.delete(law)
    db.commit()


def list_all(
    db: Session, *, page: int = 1, page_size: int = 50, archived: bool = False
) -> Page[Law]:
    """Laws for the administration, paginated. Archived ones are a separate view.

    Ordered by `created_at`, deliberately unlike the public listings: this table
    is where somebody goes back to the law they were just working on, and "most
    recently saved first" is the order that puts it at the top. The public pages
    rank by what the law *is*; this one ranks by what the administrator did last.
    """
    stmt = select(Law).where(
        Law.archived_at.is_not(None) if archived else Law.archived_at.is_(None)
    )

    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.options(*_TITLES_ONLY_DEFERRED)
            .order_by(Law.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def count_archived(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Law).where(Law.archived_at.is_not(None))) or 0
