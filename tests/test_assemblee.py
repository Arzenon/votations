"""Import of promulgated laws from the Assemblée nationale open data."""

import base64
import io
import json
import zipfile
from datetime import date

from app import assemblee
from app.config import get_settings
from app.crud import imports as imports_crud
from app.models import STATUS_IGNORED, STATUS_NEW, ImportedLaw
from tests.helpers import captured_logs


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin_auth() -> dict:
    s = get_settings()
    return _basic(s.admin_username, s.admin_password)


def _prom_acte(law_code: str, nor: str, day: int) -> dict:
    """A promulgation acte as the export publishes it."""
    return {
        "uid": f"L17-VD{law_code}PL",
        "codeActe": "PROM-PUB",
        "dateActe": f"2026-06-{day:02d}T00:00:00.000+02:00",
        "infoJO": {
            "typeJO": "JO_LOI_DECRET",
            # The JO date uses the offset-only form, unlike dateActe.
            "dateJO": f"2026-06-{day + 1:02d}+02:00",
            "pageJO": None,
            "numJO": "147",
            "urlLegifrance": f"http://www.legifrance.gouv.fr/WAspad/UnTexteDeJorf?numjo={nor}",
            "referenceNOR": nor,
        },
        "codeLoi": law_code,
        "titreLoi": f"visant à faire la chose {law_code}",
    }


def _dossier(uid: str, titre: str, actes: object) -> dict:
    return {
        "dossierParlementaire": {
            "uid": uid,
            "legislature": "17",
            "titreDossier": {"titre": titre},
            "procedureParlementaire": {"code": "2", "libelle": "Proposition de loi ordinaire"},
            "actesLegislatifs": {"acteLegislatif": actes},
        }
    }


def _zip(entries: dict[str, object]) -> bytes:
    """Build an in-memory copy of the export archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for uid, payload in entries.items():
            body = payload if isinstance(payload, str) else json.dumps(payload)
            archive.writestr(f"{assemblee.DOSSIER_PREFIX}{uid}.json", body)
    return buf.getvalue()


# The export is an XML→JSON conversion: a single acte collapses from a list to a
# bare object, and actes nest inside actes. Both shapes must parse identically.
SINGLE_ACTE = _prom_acte("2026-532", "SPOX2409232L", 24)
NESTED_ACTES = [
    {"uid": "L17-AN1", "codeActe": "AN1-DEPOT", "dateActe": None},
    {
        "uid": "L17-PROM",
        "codeActe": "PROM",
        "dateActe": None,
        "actesLegislatifs": {"acteLegislatif": _prom_acte("2026-533", "INTX2500001L", 25)},
    },
]
NOT_PROMULGATED = {"uid": "L17-AN1", "codeActe": "AN1-DEPOT", "dateActe": None}

FIXTURE = {
    "DLR5L17N001": _dossier("DLR5L17N001", "Loi sur les moniteurs de ski", SINGLE_ACTE),
    "DLR5L17N002": _dossier("DLR5L17N002", "Loi sur autre chose", NESTED_ACTES),
    "DLR5L17N003": _dossier("DLR5L17N003", "Texte encore en navette", NOT_PROMULGATED),
}


def _fetch(entries=None, since=""):
    """Parse an in-memory archive, with the given import cutoff."""
    blob = _zip(FIXTURE if entries is None else entries)
    settings = get_settings()
    previous = settings.an_import_since
    settings.an_import_since = since
    try:
        return assemblee.fetch_promulgated_laws(get=lambda url: blob)
    finally:
        settings.an_import_since = previous


def test_parses_promulgated_laws_whatever_the_collection_shape():
    laws = _fetch()

    # Only the two promulgated dossiers, newest first; the one still in navette is skipped.
    assert [law.law_code for law in laws] == ["2026-533", "2026-532"]

    nested, single = laws
    # Found through a nested acte, where acteLegislatif is a bare object.
    assert nested.dossier_uid == "DLR5L17N002"
    assert nested.promulgated_on == date(2026, 6, 25)
    # Found where the whole collection collapsed to a single object.
    assert single.dossier_uid == "DLR5L17N001"
    assert single.promulgated_on == date(2026, 6, 24)
    assert single.jo_date == date(2026, 6, 25)  # offset-only date form
    assert single.jo_number == "147"
    assert single.nor == "SPOX2409232L"
    assert single.legifrance_url.endswith("numjo=SPOX2409232L")
    assert single.title == "Loi sur les moniteurs de ski"
    assert single.procedure == "Proposition de loi ordinaire"
    assert single.dossier_url == ("https://www.assemblee-nationale.fr/dyn/17/dossiers/DLR5L17N001")


def test_import_since_filters_older_promulgations():
    laws = _fetch(since="2026-06-25")
    assert [law.law_code for law in laws] == ["2026-533"]


def test_malformed_dossier_is_skipped_not_fatal():
    entries = dict(FIXTURE)
    entries["DLR5L17N004"] = "{ this is not json"
    entries["DLR5L17N005"] = {"somethingElse": {}}
    laws = _fetch(entries)
    assert [law.law_code for law in laws] == ["2026-533", "2026-532"]


def test_raises_on_network_error():
    def boom(url):
        raise assemblee.AssembleeFetchError("network down")

    try:
        assemblee.fetch_promulgated_laws(get=boom)
        raise AssertionError("expected AssembleeFetchError")
    except assemblee.AssembleeFetchError:
        pass


def test_raises_on_corrupt_archive():
    try:
        assemblee.fetch_promulgated_laws(get=lambda url: b"not a zip file")
        raise AssertionError("expected AssembleeFetchError")
    except assemblee.AssembleeFetchError:
        pass


def test_raises_when_archive_has_no_dossiers():
    try:
        assemblee.fetch_promulgated_laws(get=lambda url: _zip({}))
        raise AssertionError("expected AssembleeFetchError")
    except assemblee.AssembleeFetchError:
        pass


def test_sync_is_idempotent_and_preserves_admin_decisions(db_session):
    records = _fetch()

    created, refreshed = imports_crud.sync(db_session, records=records)
    assert (created, refreshed) == (2, 0)

    # A second run of the same export creates nothing.
    created, refreshed = imports_crud.sync(db_session, records=records)
    assert (created, refreshed) == (0, 2)
    assert imports_crud.list_by_status(db_session)[1] == 2

    # An admin sets one aside; the next import must not undo that.
    item = imports_crud.list_by_status(db_session, status=STATUS_NEW)[0][0]
    imports_crud.set_status(db_session, item, status=STATUS_IGNORED)
    imports_crud.sync(db_session, records=records)

    db_session.expire_all()
    assert db_session.get(ImportedLaw, item.id).status == STATUS_IGNORED
    assert imports_crud.count_new(db_session) == 1


def test_sync_refreshes_factual_fields(db_session):
    records = _fetch()
    imports_crud.sync(db_session, records=records)

    records[0].title = "Titre corrigé par l'Assemblée"
    imports_crud.sync(db_session, records=records)

    titles = {item.title for item in imports_crud.list_by_status(db_session)[0]}
    assert "Titre corrigé par l'Assemblée" in titles


def test_admin_imports_page_lists_and_ignores(client, db_session):
    imports_crud.sync(db_session, records=_fetch())

    r = client.get("/admin/imports", headers=_admin_auth())
    assert r.status_code == 200
    assert "LOI n° 2026-532" in r.text
    assert "Loi sur les moniteurs de ski" in r.text

    item = imports_crud.list_by_status(db_session, status=STATUS_NEW)[0][0]
    r = client.post(
        f"/admin/imports/{item.id}/ignorer", headers=_admin_auth(), follow_redirects=False
    )
    assert r.status_code == 303
    r = client.get("/admin/imports", headers=_admin_auth())
    assert item.law_code not in r.text


def test_admin_imports_page_requires_auth(client):
    assert client.get("/admin/imports").status_code == 401


def test_creating_a_law_from_an_import_prefills_and_links(client, db_session):
    imports_crud.sync(db_session, records=_fetch())
    item = next(i for i in imports_crud.list_by_status(db_session)[0] if i.law_code == "2026-532")

    # The form carries the factual fields only — the narrative blocks stay empty.
    r = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin_auth())
    assert r.status_code == 200
    assert "LOI n° 2026-532 du 24 juin 2026" in r.text
    assert "Loi sur les moniteurs de ski" in r.text
    assert f'name="import_id" value="{item.id}"' in r.text

    r = client.post(
        "/admin/lois",
        data={
            "title": "Loi sur les moniteurs de ski",
            "official_ref": "LOI n° 2026-532 du 24 juin 2026",
            "import_id": str(item.id),
        },
        headers=_admin_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303

    db_session.expire_all()
    stored = db_session.get(ImportedLaw, item.id)
    assert stored.status == "importe"
    assert stored.law_id is not None
    assert imports_crud.count_new(db_session) == 1


def test_unknown_import_id_is_404(client, db_session):
    r = client.get("/admin/lois/nouvelle?import_id=999", headers=_admin_auth())
    assert r.status_code == 404


def test_an_oversized_field_skips_its_row_and_keeps_the_others(db_session):
    """One dossier the columns cannot hold must not throw away the whole export.

    The export goes in as a single statement, so Postgres refuses the *statement*
    rather than the row: an over-long title used to take every other law with it,
    the job exited non-zero, and the container retried in a minute — for ever, on
    a value the Assemblée nationale was not going to change on our account.
    """
    records = _fetch()
    limit = ImportedLaw.__table__.c["title"].type.length
    records[0].title = "T" * (limit + 1)

    with captured_logs() as logs:
        created, refreshed = imports_crud.sync(db_session, records=records)

    assert (created, refreshed) == (1, 0), "the good law still landed"
    titles = {item.title for item in imports_crud.list_by_status(db_session)[0]}
    assert not any(t.startswith("TTT") for t in titles), "the oversized row was not written"
    said = "\n".join(logs)
    assert records[0].dossier_uid in said, "the skipped dossier must be named"
    assert "title" in said, "the log must name the field that did not fit"


def test_a_row_that_fits_exactly_is_kept(db_session):
    """The check is a refusal of what cannot be stored, not a margin of its own."""
    records = _fetch()
    limit = ImportedLaw.__table__.c["title"].type.length
    records[0].title = "T" * limit

    created, _refreshed = imports_crud.sync(db_session, records=records)
    assert created == 2


def test_a_dossier_larger_than_the_cap_is_not_read(db_session):
    """A zip member declares its own uncompressed size; read that, not the member."""
    huge = json.dumps(
        {"dossierParlementaire": {"uid": "X", "padding": "p" * (assemblee.MAX_DOSSIER_BYTES + 10)}}
    )
    blob = _zip({"huge": huge})
    # No promulgation acte anywhere, so the honest answer is an empty list rather
    # than an error — the point is that it got there without inflating the member.
    assert assemblee.fetch_promulgated_laws(get=lambda url: blob) == []
