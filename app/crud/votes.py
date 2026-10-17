"""Recording votes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.crud import results
from app.models import Law, Vote, VoteChoice


class VotingClosed(Exception):
    """Raised when voting on a law that is not open."""


def get_existing(db: Session, law_id: int, voter_token: str) -> Vote | None:
    return db.scalar(select(Vote).where(Vote.law_id == law_id, Vote.voter_token == voter_token))


def cast_vote(
    db: Session, law: Law, voter_token: str, choice: VoteChoice
) -> tuple[Vote, str | None]:
    """Record or update a vote (one per voter per law, changeable while open).

    Returns the vote and its previous choice, or None on a first ballot. The
    previous choice is needed to move the demographic counters.

    Does not commit: the caller commits once the counters are updated too.
    The row is locked until then, so concurrent ballots from the same browser
    (double click, retried POST) can't both count the voter's profile.
    """
    if not law.is_open():
        raise VotingClosed(law.slug)

    # Invalidate the cached tally before writing, since the caller commits.
    results.forget(law.id)

    # DO NOTHING rather than DO UPDATE so we can read the previous choice below.
    # A returned id means this is the first ballot.
    inserted = db.execute(
        pg_insert(Vote)
        .values(law_id=law.id, voter_token=voter_token, choice=choice.value)
        .on_conflict_do_nothing(constraint="uq_vote_law_voter")
        .returning(Vote.id)
    ).scalar_one_or_none()

    # populate_existing: don't trust a stale copy in the identity map.
    vote = db.scalar(
        select(Vote)
        .where(Vote.law_id == law.id, Vote.voter_token == voter_token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if vote is None:  # pragma: no cover - the row was just inserted or already there
        raise RuntimeError("vote disparu juste après son enregistrement")

    if inserted is not None:
        return vote, None

    previous = vote.choice
    vote.choice = choice.value
    db.flush()
    return vote, previous
