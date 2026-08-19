"""Recording votes (privacy-preserving)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Law, Vote, VoteChoice


class VotingClosed(Exception):
    """Raised when attempting to vote on a law that is not open."""


def get_existing(db: Session, law_id: int, voter_token: str) -> Vote | None:
    return db.scalar(select(Vote).where(Vote.law_id == law_id, Vote.voter_token == voter_token))


def cast_vote(db: Session, law: Law, voter_token: str, choice: VoteChoice) -> Vote:
    """Record (or update) a vote. One vote per voter per law; changeable while open.

    Raises :class:`VotingClosed` if the law is not currently open.

    Written as a single upsert rather than read-then-insert: two requests from the
    same browser (a double click, a retried POST) would otherwise race and one of
    them would hit uq_vote_law_voter and fail with a 500.
    """
    if not law.is_open():
        raise VotingClosed(law.slug)

    stmt = (
        pg_insert(Vote)
        .values(law_id=law.id, voter_token=voter_token, choice=choice.value)
        .on_conflict_do_update(constraint="uq_vote_law_voter", set_={"choice": choice.value})
        .returning(Vote.id)
    )
    vote_id = db.execute(stmt).scalar_one()
    db.commit()
    vote = db.get(Vote, vote_id)
    if vote is None:  # pragma: no cover - the row was just written
        raise RuntimeError("vote disparu juste après son enregistrement")
    db.refresh(vote)
    return vote
