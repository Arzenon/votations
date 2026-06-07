"""Recording votes (privacy-preserving)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Law, Vote, VoteChoice


class VotingClosed(Exception):
    """Raised when attempting to vote on a law that is not open."""


def get_existing(db: Session, law_id: int, voter_token: str) -> Vote | None:
    return db.scalar(
        select(Vote).where(Vote.law_id == law_id, Vote.voter_token == voter_token)
    )


def cast_vote(db: Session, law: Law, voter_token: str, choice: VoteChoice) -> Vote:
    """Record (or update) a vote. One vote per voter per law; changeable while open.

    Raises :class:`VotingClosed` if the law is not currently open.
    """
    if not law.is_open():
        raise VotingClosed(law.slug)

    vote = get_existing(db, law.id, voter_token)
    if vote is None:
        vote = Vote(law_id=law.id, voter_token=voter_token, choice=choice.value)
        db.add(vote)
    else:
        vote.choice = choice.value
    db.commit()
    db.refresh(vote)
    return vote
