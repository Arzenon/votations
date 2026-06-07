"""Export must contain NO identifying information."""

import csv
import io

from app.models import Vote


def test_csv_export_has_no_pii(client, open_law, db_session):
    db_session.add(Vote(law_id=open_law.id, voter_token="secret-token", choice="for"))
    db_session.commit()

    r = client.get(f"/loi/{open_law.slug}/export.csv")
    assert r.status_code == 200
    assert "secret-token" not in r.text

    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0].keys() == {"law_id", "choice", "created_at"}.intersection(rows[0].keys())
    assert set(rows[0].keys()) == {"law_id", "choice", "created_at"}
    assert "voter_token" not in r.text


def test_json_export_has_no_voter_token(client, open_law, db_session):
    db_session.add(Vote(law_id=open_law.id, voter_token="secret-token", choice="abstain"))
    db_session.commit()

    data = client.get(f"/loi/{open_law.slug}/export.json").json()
    assert "secret-token" not in client.get(f"/loi/{open_law.slug}/export.json").text
    for vote in data["votes"]:
        assert set(vote.keys()) == {"law_id", "choice", "created_at"}
