"""SQLAlchemy models."""

from app.models.law import Law
from app.models.vote import Vote, VoteChoice

__all__ = ["Law", "Vote", "VoteChoice"]
