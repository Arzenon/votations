"""Page rendering, admin guard, and the connexion placeholder."""

import base64

from app.config import get_settings


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin_auth() -> dict:
    s = get_settings()
    return _basic(s.admin_username, s.admin_password)


def test_home_lists_published_law(client, open_law):
    r = client.get("/")
    assert r.status_code == 200
    assert open_law.title in r.text
    assert "sondage représentatif" in r.text  # permanent disclaimer


def test_law_page_shows_four_blocks(client, open_law):
    r = client.get(f"/loi/{open_law.slug}")
    assert r.status_code == 200
    for heading in ["Ce que dit la loi", "Ce que ça change", "partisans", "opposants"]:
        assert heading in r.text


def test_unpublished_law_is_404(client, db_session):
    from app.models import Law

    law = Law(slug="brouillon", title="Brouillon", published=False)
    db_session.add(law)
    db_session.commit()
    assert client.get(f"/loi/{law.slug}").status_code == 404


def test_admin_requires_auth(client):
    assert client.get("/admin").status_code == 401


def test_admin_challenge_carries_a_realm(client):
    """RFC 7617 requires `realm`; without it Firefox never shows a login prompt."""
    # No credentials at all — the challenge comes from FastAPI's HTTPBasic.
    r = client.get("/admin")
    assert r.headers["www-authenticate"] == 'Basic realm="Votations"'
    # Wrong credentials — the challenge comes from require_admin.
    r = client.get("/admin", headers=_basic("admin", "definitely-wrong"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Votations"'


def test_admin_accepts_configured_credentials(client):
    r = client.get("/admin", headers=_admin_auth())
    assert r.status_code == 200
    assert "Administration" in r.text


def test_admin_can_create_law(client, db_session):
    from app.models import Law

    r = client.post(
        "/admin/lois",
        data={"title": "Nouvelle loi créée", "published": "1"},
        headers=_admin_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.query(Law).filter_by(title="Nouvelle loi créée").count() == 1


def test_connexion_shows_disabled_button(client):
    r = client.get("/connexion")
    assert r.status_code == 200
    assert "FranceConnect" in r.text
    assert "bientôt disponible" in r.text
