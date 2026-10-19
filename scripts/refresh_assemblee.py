"""Import newly promulgated laws from the Assemblée nationale open data.

Run by the `assemblee-refresh` service, or by hand. On a fetch error it exits
with an error and leaves the staged rows unchanged. Safe to rerun; admin
decisions are never overwritten.

    python -m scripts.refresh_assemblee
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import SQLAlchemyError

from app.assemblee import AssembleeFetchError, PromulgatedLaw, fetch_promulgated_laws
from app.crud import imports as imports_crud
from app.db import SessionLocal
from app.scrutins import ScrutinsFetchError, fetch_votes, final_vote


def _attach_votes(records: list[PromulgatedLaw]) -> tuple[str, bool]:
    """Add the Assemblée vote counts. Returns a summary line and whether the
    scrutins export was read.

    Best effort: if the scrutins download fails, the import goes on without
    counts.
    """
    wanted = {ref for record in records for ref in record.vote_refs}
    if not wanted:
        # No votes referenced: mark as checked, there was no public scrutin.
        for record in records:
            record.an_vote_checked = True
        return "aucun scrutin référencé", True
    try:
        index = fetch_votes(wanted)
    except ScrutinsFetchError as exc:
        return f"scrutins non récupérés ({exc}) - décomptes conservés", False

    resolved = 0
    for record in records:
        record.an_vote_checked = True
        vote = final_vote(record.vote_refs, index)
        if vote is None:
            continue
        record.an_vote_for = vote.pour
        record.an_vote_against = vote.contre
        record.an_vote_abstain = vote.abstentions
        resolved += 1
    return f"{resolved} scrutin(s) rattaché(s) sur {len(records)} loi(s)", True


def main() -> int:
    try:
        records = fetch_promulgated_laws()
    except AssembleeFetchError as exc:
        print(f"Import AN ignoré, échec de récupération : {exc}", file=sys.stderr)
        return 1

    votes, scrutins_read = _attach_votes(records)

    db = SessionLocal()
    propagated = 0
    try:
        created, refreshed = imports_crud.sync(db, records=records, with_scrutins=scrutins_read)
        # Also update laws already created from these rows.
        propagated = imports_crud.propagate_parliament_record(db)
    except SQLAlchemyError as exc:
        # A message and an exit code rather than a traceback.
        db.rollback()
        print(f"Import AN ignoré, erreur de base de données : {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(
        f"Lois promulguées : {created} nouvelle(s), {refreshed} actualisée(s) - {votes}"
        f" - {propagated} votation(s) mise(s) à jour"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
