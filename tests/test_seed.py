"""The sample data loader."""

from __future__ import annotations

from unittest.mock import patch

from app.crud import results as results_crud
from app.models import Law
from scripts import seed
from tests.conftest import _NoCloseSession


def test_sample_votes_carry_their_profiles(db_session):
    """The votes behind the sample counters are marked as profiled.

    They used to be picked before being written, so none were found: the page
    showed full breakdowns next to "0 sur 706" profiles.
    """
    with patch("scripts.seed.SessionLocal", lambda: _NoCloseSession(db_session)):
        seed.main()

    law = db_session.query(Law).filter_by(slug="encadrement-loyers-zones-tendues").one()
    sample = next(spec for spec in seed.SAMPLE_LAWS if spec["slug"] == law.slug)
    largest = max(
        sum(n for choices in categories.values() for n in choices.values())
        for categories in sample["demographics"].values()
    )
    assert results_crud.tally(db_session, law.id).with_profile == largest
