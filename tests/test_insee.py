"""INSEE reference: offline fetch/parse + DB cache fallback."""

import json
from urllib.parse import parse_qs, urlparse

from app import insee
from app.crud import demographics as demo_crud
from app.models import InseeReference


def _obs(dims: dict, value: float) -> dict:
    dims = {"TIME_PERIOD": "2026", **dims}
    return {
        "attributes": {},
        "dimensions": dims,
        "measures": {"OBS_VALUE_NIVEAU": {"value": value}},
    }


QUINQUENNAL = {
    "Y15T19": 500.0,
    "Y20T24": 500.0,  # 18-24 ≈ 0.4*500 + 500 = 700
    "Y25T29": 400.0,
    "Y30T34": 400.0,  # 25-34 = 800
    "Y35T39": 300.0,
    "Y40T44": 300.0,
    "Y45T49": 300.0,  # 35-49 = 900
    "Y50T54": 200.0,
    "Y55T59": 200.0,
    "Y60T64": 200.0,  # 50-64 = 600
    "Y65T69": 100.0,
    "Y_GE95": 100.0,  # 65+ = 200
    "Y_LT5": 999.0,
    "Y5T9": 999.0,
    "Y10T14": 999.0,  # minors — must be excluded
}


def fake_get(url: str) -> bytes:
    """Stand-in for the Melodi API, keyed on the GEO/SEX/AGE query parameters.

    Returns bytes, like every other injected getter in the app: Melodi answers
    JSON, but the seam is the same one the Journal Officiel and the Assemblée
    nationale use, and decoding belongs to the module that knows the format.
    """
    return json.dumps(_fake_payload(url)).encode()


def _fake_payload(url: str) -> dict:
    q = parse_qs(urlparse(url).query)
    geo = q["GEO"][0]
    _prefix, level, code = geo.split("-", 2)
    if level == "FRANCE" and code == "F":
        # Sexe slice (AGE=_T) vs age slice (SEX=_T) distinguished by the filter.
        if q.get("AGE") == ["_T"]:
            return {
                "observations": [
                    _obs({"SEX": "F", "AGE": "_T"}, 520.0),
                    _obs({"SEX": "M", "AGE": "_T"}, 480.0),
                    _obs({"SEX": "_T", "AGE": "_T"}, 1000.0),
                ]
            }
        return {"observations": [_obs({"SEX": "_T", "AGE": a}, v) for a, v in QUINQUENNAL.items()]}
    if level == "REG" and code in insee.REGION_CODE_TO_LABEL:
        pop = 200.0 if code == "11" else 100.0  # IDF bigger
        return {"observations": [_obs({"SEX": "_T", "AGE": "_T"}, pop)]}
    return {"observations": []}  # overseas / unknown → skipped


def test_fetch_reference_parses_sex_age_and_region():
    data = insee.fetch_reference(get=fake_get)
    assert data.source_year == 2026
    assert data.values["sexe"] == {"femme": 52.0, "homme": 48.0}

    # Age: brackets are quinquennal sums (18-24 prorates 15-19), minors excluded.
    age = data.values["age"]
    assert set(age) == {"18-24", "25-34", "35-49", "50-64", "65+"}
    assert abs(sum(age.values()) - 100.0) < 0.5
    adults = 700 + 800 + 900 + 600 + 200  # = 3200, no minors
    assert age["25-34"] == round(100 * 800 / adults, 1)
    assert age["18-24"] == round(100 * 700 / adults, 1)

    region = data.values["region"]
    assert len(region) == len(insee.REGION_CODE_TO_LABEL)  # 13 metro, no overseas
    assert abs(sum(region.values()) - 100.0) < 1.0  # normalised (rounding)
    assert max(region, key=region.get) == "Île-de-France"  # the bigger one
    assert "Outre-mer" not in region


def test_fetch_reference_raises_on_unreachable():
    def boom(url):
        raise insee.InseeFetchError("network down")

    try:
        insee.fetch_reference(get=boom)
        raise AssertionError("expected InseeFetchError")
    except insee.InseeFetchError:
        pass


def _reference(db, dimension):
    return demo_crud.all_references(db)[dimension]


def test_get_reference_prefers_cache_then_falls_back(db_session, open_law):
    # No rows → curated fallback constant, no source year.
    mapping, year = _reference(db_session, "sexe")
    assert year is None
    assert mapping["femme"] == 51.5  # from INSEE_REFERENCE constant

    # With a cached row → DB value + source year.
    db_session.add(
        InseeReference(dimension="sexe", category="femme", percentage=49.9, source_year=2026)
    )
    db_session.commit()
    mapping, year = _reference(db_session, "sexe")
    assert year == 2026
    assert mapping["femme"] == 49.9
