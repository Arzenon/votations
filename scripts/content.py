"""Export and re-import the editorial content: the laws and their public record.

This is **not** the backup. ``scripts/backup.sh`` dumps the whole database and is
what protects the votes, which are the irreplaceable part and are deliberately
absent from here. This is the other need: moving an instance to another host, or
seeding a fresh one, without rewriting by hand the neutral presentations that are
the actual work on this platform.

So it is a portable file rather than a dump — readable, diffable, and restorable
into a database at a different migration revision, which a ``pg_dump`` is not.

What it carries, and why:

* **the laws**, published, draft and archived alike. A presentation in progress
  is exactly the thing whose loss costs a day of writing.
* **the public record of changes** (``law_revisions``). Not an optimisation: the
  results page reads it to tell a visitor whether the text moved while people
  were voting. Restore the laws without it and the new site states « aucune
  modification depuis la publication » about texts that were modified — the
  charter's transparency claim, quietly turned into a false one.
* **the review decisions** taken on imported laws — which were set aside, and
  which produced a votation. The daily import rebuilds the facts on its own but
  never touches these, by design, so nothing else would bring them back.

What it deliberately omits: the votes and their demographic counters (the dump's
job, and a reduced copy here would invite mistaking one for the other), the INSEE
reference shares (refetched monthly), and the cached AI drafts (regenerable, at
the price of another model call).

Identifiers are not carried across: a row's ``id`` means nothing in another
database. Laws are keyed on their slug, imported laws on their dossier uid.

    python -m scripts.content export [--dir /exports] [--keep-days 5]
    python -m scripts.content import fichier.json [--replace]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crud.laws import slugify
from app.db import SessionLocal, utcnow
from app.models import ImportedLaw, Law, LawRevision

#: Bumped when the shape below changes in a way an older importer cannot read.
#:
#: *Adding* a field is not that, and must not bump it. The reader below refuses
#: any version but its own, so a bump makes every snapshot already on disk
#: unreadable — and the reason five days of them are kept is to be able to go
#: back to one. A field the old writer never wrote simply arrives missing and
#: restores as empty, which is what it was.
FORMAT_VERSION = 1

#: Everything an administrator wrote or scheduled.
LAW_FIELDS = (
    "slug",
    "title",
    "official_ref",
    "legifrance_url",
    "summary_text",
    "practical_changes",
    "arguments_for",
    "arguments_against",
    "debate_summary",
    "an_vote_for",
    "an_vote_against",
    "an_vote_abstain",
    "cmp_convened",
    "an_vote_checked",
    "promulgated_on",
    "published",
    "opens_at",
    "closes_at",
    "archived_at",
    "created_at",
)

#: Columns of ``laws`` that deliberately do not travel, each for a stated reason.
#: Together with :data:`LAW_FIELDS` this must account for every column, which
#: ``test_every_column_of_a_law_is_carried_or_deliberately_left_out`` checks —
#: because the way this file goes wrong is by silence. ``promulgated_on`` was
#: added as a column, as an admin form field and as the home page's sort key, and
#: was simply not added here: a snapshot restored on another host put every law's
#: promulgation date back as empty, and nothing anywhere said so.
_NOT_CARRIED = frozenset(
    {
        # Means nothing in another database — see the module docstring.
        "id",
        # Set by the write that restores the row. Carrying the old value would
        # state that a law had not been touched since a date that precedes its
        # own insertion, which is the one thing the column is read for.
        "updated_at",
        # Bookkeeping for the published demographic breakdown, which is derived
        # from the votes — and the votes are the dump's job, not this file's. A
        # restored law starts with no snapshot, which is exactly right: there is
        # nothing published about a votation whose ballots did not travel.
        "demographics_snapshot_at",
        "demographics_snapshot_profiles",
    }
)

REVISION_FIELDS = ("changed_at", "field", "old_value", "new_value")


#: The fields that hold an instant rather than text. Named rather than sniffed:
#: reading every string back through `fromisoformat` turned a slug into a parse
#: error, and would have turned an unlucky title into a date.
_INSTANT_FIELDS = frozenset({"opens_at", "closes_at", "archived_at", "created_at", "changed_at"})

#: And the ones that hold a calendar day. Kept apart from the instants because
#: they read back differently: `datetime.fromisoformat` on « 2026-06-24 » answers
#: midnight on that day, which is an instant nobody recorded and which a timezone
#: can then move to the day before.
_DAY_FIELDS = frozenset({"promulgated_on"})


def _plain(name: str, value: Any) -> Any:
    """JSON-safe, keeping instants and days unambiguous.

    `datetime` is checked before `date` because it subclasses it — the same
    ordering `app.crud.laws._as_text` needs, for the same reason.
    """
    if name in _INSTANT_FIELDS and isinstance(value, datetime):
        return value.isoformat()
    if name in _DAY_FIELDS and isinstance(value, date):
        return value.isoformat()
    return value


def _restore(name: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if name in _INSTANT_FIELDS:
        return datetime.fromisoformat(value)
    if name in _DAY_FIELDS:
        return date.fromisoformat(value)
    return value


# --- Export -------------------------------------------------------------------


def build_document(db: Session) -> dict:
    """The whole editorial content of this instance, as one JSON-able mapping."""
    laws = list(db.scalars(select(Law).order_by(Law.created_at)))
    by_id = {law.id: law.slug for law in laws}

    revisions: dict[str, list[dict]] = {}
    for revision in db.scalars(select(LawRevision).order_by(LawRevision.changed_at)):
        slug = by_id.get(revision.law_id)
        if slug is None:  # pragma: no cover - the FK cascades, so this cannot happen
            continue
        revisions.setdefault(slug, []).append(
            {name: _plain(name, getattr(revision, name)) for name in REVISION_FIELDS}
        )

    decisions = [
        {
            "dossier_uid": item.dossier_uid,
            "source": item.source,
            "status": item.status,
            # The slug, not the id: ids do not survive the move.
            "law_slug": by_id.get(item.law_id) if item.law_id else None,
        }
        for item in db.scalars(select(ImportedLaw).order_by(ImportedLaw.dossier_uid))
    ]

    return {
        "format": FORMAT_VERSION,
        "generated_at": utcnow().isoformat(),
        "laws": [
            {
                **{name: _plain(name, getattr(law, name)) for name in LAW_FIELDS},
                "revisions": revisions.get(law.slug, []),
            }
            for law in laws
        ],
        "import_decisions": decisions,
    }


class ExportDirectoryError(RuntimeError):
    """The snapshot directory cannot be written to."""


#: Suffix of a snapshot still being written. Named rather than spelled out at the
#: two places that need it: the retention sweep below globs for what it collects,
#: and a glob that does not know this suffix collects everything *except* the
#: files that are actually junk.
_PARTIAL_SUFFIX = ".json.partial"


def export(directory: Path, keep_days: int) -> Path:
    """Write today's snapshot and drop the ones past ``keep_days``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".ecriture"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        # The predictable failure of a bind mount: the directory on the host
        # belongs to somebody else, so the container's user cannot write into it.
        raise ExportDirectoryError(
            f"{directory} n'est pas accessible en écriture ({exc}). Sur un montage "
            "lié, vérifiez que le dossier existe côté hôte et que le conteneur "
            "tourne sous le même utilisateur - voir EXPORT_UID dans DEPLOY.md."
        ) from exc
    db = SessionLocal()
    try:
        document = build_document(db)
    finally:
        db.close()

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    target = directory / f"votations-contenu-{stamp}.json"
    # Written beside and renamed, so an interrupted run never leaves a truncated
    # file that looks like a snapshot.
    partial = target.with_suffix(_PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.rename(target)

    if keep_days > 0:
        cutoff = utcnow().timestamp() - keep_days * 86400
        for old in directory.glob("votations-contenu-*.json"):
            if old != target and old.stat().st_mtime < cutoff:
                old.unlink()

    # What a killed run leaves behind. The sweep above collects snapshots, and a
    # `.json.partial` is not one — so an interrupted export used to sit in the
    # bind mount for ever, full size, on the same disk as PostgreSQL. An hour
    # old, so a run happening right now is never touched.
    stale = utcnow().timestamp() - 3600
    for junk in directory.glob(f"votations-contenu-*{_PARTIAL_SUFFIX}"):
        if junk.stat().st_mtime < stale:
            junk.unlink()
    return target


# --- Import -------------------------------------------------------------------


class ContentFormatError(RuntimeError):
    """The file is not a snapshot this version knows how to read."""


class PublishedLawError(RuntimeError):
    """``--replace`` was asked to overwrite a law that is currently published."""


def _read(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContentFormatError(f"fichier illisible : {exc}") from exc
    if not isinstance(document, dict) or "laws" not in document:
        raise ContentFormatError("ce fichier n'est pas un export de contenu")
    version = document.get("format")
    if version != FORMAT_VERSION:
        raise ContentFormatError(f"format {version} — cette version lit le format {FORMAT_VERSION}")
    return document


def _import_law(db: Session, entry: dict, *, replace: bool) -> str:
    """Insert one law and its revisions. Returns what was done."""
    raw_slug = entry.get("slug")
    if not raw_slug:
        raise ContentFormatError("une loi sans slug ne peut pas être identifiée")
    # Re-derived rather than trusted. A slug is a URL segment and reaches a
    # `Content-Disposition` header on the export routes, and this file is the one
    # input to the application that arrives as a whole document from wherever the
    # operator got it. `slugify` is idempotent on anything it produced, so a
    # snapshot of a healthy instance round-trips unchanged.
    slug = slugify(raw_slug)

    existing = db.scalar(select(Law).where(Law.slug == slug))
    if existing is not None and not replace:
        # Skipping rather than merging is what makes a second run harmless.
        return "skipped"
    if existing is not None and existing.published:
        # `--replace` writes straight to the columns, so it records nothing in
        # `law_revisions` — the public history would keep saying the text never
        # moved while the text moved. On a published law that is not a bug to fix
        # later, it is the transparency claim being made false in one command.
        # Editing a published law goes through /admin, where it is recorded.
        raise PublishedLawError(
            f"« {slug} » est publiée : la remplacer réécrirait le texte sur lequel "
            "des gens ont voté sans l'inscrire à l'historique public. Modifiez-la "
            "depuis /admin, ou dépubliez-la d'abord."
        )

    columns = {name: _restore(name, entry.get(name)) for name in LAW_FIELDS}
    columns["slug"] = slug
    if existing is None:
        law = Law(**columns)
        db.add(law)
        outcome = "created"
    else:
        for name, value in columns.items():
            setattr(existing, name, value)
        law = existing
        outcome = "replaced"
    db.flush()  # assign the id the revisions below need

    known = {
        (revision.changed_at, revision.field)
        for revision in db.scalars(select(LawRevision).where(LawRevision.law_id == law.id))
    }
    for raw in entry.get("revisions") or []:
        values = {name: _restore(name, raw.get(name)) for name in REVISION_FIELDS}
        if (values["changed_at"], values["field"]) in known:
            continue
        db.add(LawRevision(law_id=law.id, **values))
    return outcome


def _apply_decisions(db: Session, decisions: list[dict]) -> tuple[int, int]:
    """Re-apply the review decisions. Returns (applied, not yet importable).

    A decision can only be attached to a staged row that exists, and those are
    rebuilt by the daily import job rather than by this file. Anything it has not
    seen yet is reported rather than invented: the alternative would be writing
    half a row, with the factual columns guessed.
    """
    applied = pending = 0
    for decision in decisions:
        item = db.scalar(
            select(ImportedLaw).where(
                ImportedLaw.source == decision.get("source", "assemblee"),
                ImportedLaw.dossier_uid == decision.get("dossier_uid"),
            )
        )
        if item is None:
            pending += 1
            continue
        item.status = decision.get("status", item.status)
        slug = decision.get("law_slug")
        if slug:
            # Through `slugify` for the same reason the laws are, and so that a
            # decision still finds the law it points at after that pass.
            law = db.scalar(select(Law).where(Law.slug == slugify(slug)))
            item.law_id = law.id if law is not None else None
        applied += 1
    return applied, pending


def import_content(path: Path, *, replace: bool = False) -> dict[str, int]:
    """Load a snapshot into this database. Idempotent unless ``replace``."""
    document = _read(path)
    counts = {"created": 0, "replaced": 0, "skipped": 0}
    db = SessionLocal()
    try:
        for entry in document["laws"]:
            counts[_import_law(db, entry, replace=replace)] += 1
        applied, pending = _apply_decisions(db, document.get("import_decisions") or [])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {**counts, "decisions": applied, "decisions_pending": pending}


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verbs = parser.add_subparsers(dest="verb", required=True)

    out = verbs.add_parser("export", help="écrire un instantané du contenu")
    out.add_argument("--dir", type=Path, default=Path("/exports"))
    out.add_argument("--keep-days", type=int, default=5)

    back = verbs.add_parser("import", help="recharger un instantané")
    back.add_argument("path", type=Path)
    back.add_argument(
        "--replace",
        action="store_true",
        help="écraser une loi dont le slug existe déjà (par défaut : ignorée)",
    )

    args = parser.parse_args(argv)
    if args.verb == "export":
        try:
            target = export(args.dir, args.keep_days)
        except ExportDirectoryError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        size = target.stat().st_size
        print(f"→ contenu exporté : {target} ({size} octets)")
        return 0

    try:
        counts = import_content(args.path, replace=args.replace)
    except (ContentFormatError, PublishedLawError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(
        "→ {created} loi(s) créée(s), {replaced} remplacée(s), {skipped} ignorée(s) ; "
        "{decisions} décision(s) d'import réappliquée(s)".format(**counts)
    )
    if counts["decisions_pending"]:
        print(
            f"  {counts['decisions_pending']} décision(s) en attente : les lois "
            "correspondantes n'ont pas encore été vues par l'import quotidien. "
            "Relancez cette commande après son prochain passage."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
