"""Import newly promulgated laws from the Assemblée nationale open data.

Run manually or on a schedule (see the `assemblee-refresh` service in
docker-compose). On any fetch error it exits non-zero **without touching** the
staged rows, so the admin keeps seeing the last good list. Safe to run
repeatedly: rows are keyed on the dossier identifier, and an administrator's
decision on a row is never overwritten.

    python -m scripts.refresh_assemblee
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import SQLAlchemyError

from app.assemblee import AssembleeFetchError, PromulgatedLaw, fetch_promulgated_laws
from app.crud import imports as imports_crud
from app.db import SessionLocal
from app.scrutins import ScrutinsFetchError, fetch_votes, final_vote


def _attach_votes(records: list[PromulgatedLaw]) -> str:
    """Fill in how the Assemblée voted. Returns a line for the run's output.

    **Best effort, and deliberately so.** The counts live in a second export, and
    a law without them is a law the site can still present - it simply says the
    text was adopted without a public scrutin, which is true of a third of them
    anyway. Losing the whole import because that download failed would trade
    something valuable for something merely nice.
    """
    wanted = {ref for record in records for ref in record.vote_refs}
    if not wanted:
        # Looked, and no dossier references a vote at all. That is a finding, not
        # a failure, so the records are marked as checked: the page may say these
        # texts were adopted without a public scrutin.
        for record in records:
            record.an_vote_checked = True
        return "aucun scrutin référencé"
    try:
        index = fetch_votes(wanted)
    except ScrutinsFetchError as exc:
        # `an_vote_checked` stays false on every record, which is the whole point
        # of that column: the page says nothing about a vote nobody has looked up.
        return f"scrutins non récupérés ({exc}) - décomptes laissés vides"

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
    return f"{resolved} scrutin(s) rattaché(s) sur {len(records)} loi(s)"


def main() -> int:
    try:
        records = fetch_promulgated_laws()
    except AssembleeFetchError as exc:
        print(f"Import AN ignoré — échec de récupération : {exc}", file=sys.stderr)
        return 1

    votes = _attach_votes(records)

    db = SessionLocal()
    propagated = 0
    try:
        created, refreshed = imports_crud.sync(db, records=records)
        # And onto the votations already made from these rows. The copy used to
        # happen only at creation, so a law created before a scrutin was
        # published never saw it - see `propagate_parliament_record`.
        propagated = imports_crud.propagate_parliament_record(db)
    except SQLAlchemyError as exc:
        # Every database refusal, not just the two that mean "the schema is not
        # migrated yet". The narrower pair let a `DataError` — a value the
        # columns could not hold — escape as a traceback, which the containerised
        # loop reads as a crash and retries in a minute, for ever. `sync` now
        # keeps such rows out, so reaching here means something else; it is still
        # a message and an exit code rather than a stack trace.
        db.rollback()
        print(f"Import AN ignoré — base de données : {exc}", file=sys.stderr)
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
