"""Export and re-import the editorial content: laws and their public history.

This is not the backup (that's ``scripts/backup.sh``, which covers the votes).
It's a readable JSON file for moving an instance or seeding a new one, and can
be restored on a database at another migration revision.

Included: all laws (published, drafts, archived), their revision history (the
results page relies on it) and the review decisions on imported laws.
Not included: votes and demographic counters (in the backup), INSEE data
(refetched) and AI drafts (can be regenerated).

Ids aren't carried over: laws are keyed by slug, imported laws by dossier uid.

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

from app.crud.laws import moves_final_closing, slugify
from app.db import SessionLocal, utcnow
from app.models import ImportedLaw, Law, LawRevision

# Only bump for incompatible changes, not for added fields: the importer
# rejects other versions, which would make existing snapshots unreadable.
FORMAT_VERSION = 1

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

# Columns not exported. With LAW_FIELDS this must cover every column (checked
# by a test), so a new column can't be forgotten silently.
_NOT_CARRIED = frozenset(
    {
        "id",
        # Set when the row is restored.
        "updated_at",
        # Derived from the votes, which aren't exported.
        "demographics_snapshot_at",
        "demographics_snapshot_profiles",
    }
)

REVISION_FIELDS = ("changed_at", "field", "old_value", "new_value")


# Datetime fields, listed explicitly rather than guessed from the values.
_INSTANT_FIELDS = frozenset({"opens_at", "closes_at", "archived_at", "created_at", "changed_at"})

# Date fields, parsed as dates (not as midnight datetimes).
_DAY_FIELDS = frozenset({"promulgated_on"})


def _plain(name: str, value: Any) -> Any:
    """Convert dates and datetimes to ISO strings for JSON."""
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
    """All the editorial content, as a JSON-serialisable dict."""
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


# Suffix of a snapshot being written.
_PARTIAL_SUFFIX = ".json.partial"


def export(directory: Path, keep_days: int) -> Path:
    """Write today's snapshot and drop the ones past ``keep_days``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".ecriture"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        # Typically a bind mount owned by another user.
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
    # Write then rename, so an interrupted run never leaves a truncated snapshot.
    partial = target.with_suffix(_PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.rename(target)

    if keep_days > 0:
        cutoff = utcnow().timestamp() - keep_days * 86400
        for old in directory.glob("votations-contenu-*.json"):
            if old != target and old.stat().st_mtime < cutoff:
                old.unlink()

    # Delete leftover partial files from interrupted runs (older than an hour,
    # so a running export isn't affected).
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


class FinalClosingError(RuntimeError):
    """``--replace`` would move the closing of a closed vote that has votes."""


def _read(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContentFormatError(f"fichier illisible : {exc}") from exc
    if not isinstance(document, dict) or "laws" not in document:
        raise ContentFormatError("ce fichier n'est pas un export de contenu")
    version = document.get("format")
    if version != FORMAT_VERSION:
        raise ContentFormatError(
            f"format {version} non pris en charge (cette version lit le format {FORMAT_VERSION})"
        )
    return document


def _import_law(db: Session, entry: dict, *, replace: bool) -> str:
    """Insert one law and its revisions. Returns what was done."""
    raw_slug = entry.get("slug")
    if not raw_slug:
        raise ContentFormatError("une loi sans slug ne peut pas être identifiée")
    # Don't trust the file: the slug ends up in URLs and headers. slugify is
    # idempotent, so valid slugs are unchanged.
    slug = slugify(raw_slug)

    existing = db.scalar(select(Law).where(Law.slug == slug))
    if existing is not None and not replace:
        # Skip, so running twice is harmless.
        return "skipped"
    if existing is not None and existing.published:
        # --replace doesn't record revisions, so a published law would change
        # without trace in its public history. Edit it in /admin instead.
        raise PublishedLawError(
            f"« {slug} » est publiée : la remplacer réécrirait le texte sur lequel "
            "des gens ont voté sans l'inscrire à l'historique public. Modifiez-la "
            "depuis /admin, ou dépubliez-la d'abord."
        )

    columns = {name: _restore(name, entry.get(name)) for name in LAW_FIELDS}
    columns["slug"] = slug
    if existing is not None and moves_final_closing(db, existing, columns["closes_at"]):
        raise FinalClosingError(
            f"« {slug} » est close et a reçu des votes : sa date de clôture ne peut "
            "plus changer. La rouvrir permettrait de comparer ses résultats de "
            "clôture aux suivants, et d'en isoler un vote."
        )
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
    """Re-apply the review decisions. Returns (applied, pending).

    Staged rows are recreated by the daily import, not by this script.
    Decisions for rows not imported yet are counted as pending.
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
            # slugify, as for the laws above.
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
    except (ContentFormatError, PublishedLawError, FinalClosingError) as exc:
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
