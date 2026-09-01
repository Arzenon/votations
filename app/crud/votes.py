"""Recording votes (privacy-preserving)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.crud import results
from app.models import Law, Vote, VoteChoice


class VotingClosed(Exception):
    """Raised when attempting to vote on a law that is not open."""


def get_existing(db: Session, law_id: int, voter_token: str) -> Vote | None:
    return db.scalar(select(Vote).where(Vote.law_id == law_id, Vote.voter_token == voter_token))


def cast_vote(
    db: Session, law: Law, voter_token: str, choice: VoteChoice
) -> tuple[Vote, str | None]:
    """Record (or update) a vote. One vote per voter per law; changeable while open.

    Returns the row and **the choice it held before this ballot**, or None when
    this browser had not voted on this law. That second value is what lets the
    demographic counters be moved without the vote row having to remember where
    a contribution was filed — see :func:`app.crud.demographics.record`.

    Raises :class:`VotingClosed` if the law is not currently open.

    Commits nothing. The ballot is one transaction — the choice and the
    counters it moves are written or neither is — and the caller closes it.

    The row is inserted without touching an existing one, then **locked** and
    updated. Both halves of that matter:

    * inserting with ``DO NOTHING`` rather than ``DO UPDATE`` means the value
      being overwritten is a value this transaction has read, which is the only
      way to know what to decrement;
    * the lock is held until the caller commits, so two ballots arriving from
      one browser at once — a double click, a retried POST, two tabs — are
      serialised. Without it both read ``demographics_recorded`` as false and
      both added a profile to the counters, and one voter counted twice.
    """
    if not law.is_open():
        raise VotingClosed(law.slug)

    # The figures for this law have just stopped being what anybody remembered.
    # See `app.crud.results.forget`: the listings memoise a tally for as long as
    # their own Cache-Control already promises, and this is what keeps that a
    # backstop rather than the mechanism — one ballot forgets one law, so a busy
    # site re-counts the votation being voted on and reads the rest from memory.
    #
    # Before the write rather than after, because the write is not this
    # function's to finish: the ballot is one transaction and the caller commits
    # it. The cost of being early is a reader landing in between and storing the
    # previous figures — for the length of a window the page has already declared.
    results.forget(law.id)

    # RETURNING yields no row when the insert was skipped, which is exactly the
    # signal we want: a row came back ⇒ this is the first ballot from this
    # browser on this law.
    inserted = db.execute(
        pg_insert(Vote)
        .values(law_id=law.id, voter_token=voter_token, choice=choice.value)
        .on_conflict_do_nothing(constraint="uq_vote_law_voter")
        .returning(Vote.id)
    ).scalar_one_or_none()

    # `populate_existing` because the row may have been written by Core just
    # above, or read earlier in this request: the session is
    # `expire_on_commit=False`, so a stale copy in the identity map would
    # otherwise be handed back carrying the choice it held before.
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
