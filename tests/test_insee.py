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
    "Y70T74": 0.0,
    "Y75T79": 0.0,
    "Y80T84": 0.0,
    "Y85T89": 0.0,
    "Y90T94": 0.0,
    "Y_GE95": 100.0,  # 65+ = 200
    "Y_LT5": 999.0,
    "Y5T9": 999.0,
    "Y10T14": 999.0,  # minors, must be excluded
}


def fake_get(url: str) -> bytes:
    """Fake Melodi API, keyed on the GEO/SEX/AGE query parameters. Returns bytes."""
    return json.dumps(_fake_payload(url)).encode()


def _fake_payload(url: str) -> dict:
    q = parse_qs(urlparse(url).query)
    geo = q["GEO"][0]
    _prefix, level, code = geo.split("-", 2)
    if level == "FRANCE" and code == "F":
        # Sex query (AGE=_T) vs age query (SEX=_T).
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
    if level == "REG" and code in insee.OVERSEAS_CODES:
        return {"observations": [_obs({"SEX": "_T", "AGE": "_T"}, 10.0)]}
    return {"observations": []}


def test_fetch_reference_parses_sex_age_and_region():
    data = insee.fetch_reference(get=fake_get)
    assert data.source_year == 2026
    assert data.values["sexe"] == {"femme": 52.0, "homme": 48.0}

    # Age: sums of five-year groups (18-24 takes part of 15-19), no minors.
    age = data.values["age"]
    assert set(age) == {"18-24", "25-34", "35-49", "50-64", "65+"}
    assert abs(sum(age.values()) - 100.0) < 0.5
    adults = 700 + 800 + 900 + 600 + 200  # = 3200, no minors
    assert age["25-34"] == round(100 * 800 / adults, 1)
    assert age["18-24"] == round(100 * 700 / adults, 1)

    region = data.values["region"]
    assert len(region) == len(insee.REGION_CODE_TO_LABEL) + 1  # 13 metro + overseas
    assert abs(sum(region.values()) - 100.0) < 1.0  # normalised (rounding)
    assert max(region, key=region.get) == "Île-de-France"  # the bigger one
    # The five overseas codes, summed: 50 out of 200 + 12 * 100 + 50.
    assert region["Outre-mer"] == round(100 * 50 / 1450, 1)


def test_fetch_reference_raises_on_unreachable():
    def boom(url):
        raise insee.InseeFetchError("network down")

    try:
        insee.fetch_reference(get=boom)
        raise AssertionError("expected InseeFetchError")
    except insee.InseeFetchError:
        pass


def test_a_missing_region_fails_the_refresh_instead_of_skewing_the_others():
    """Normalising without a région (here Île-de-France, then Mayotte) inflates the others."""
    import pytest

    for code in ("11", "06"):

        def without(url: str, code=code) -> bytes:
            if f"-REG-{code}" in url:
                raise insee.InseeFetchError("timeout")
            return fake_get(url)

        with pytest.raises(insee.InseeFetchError, match=f"région {code}"):
            insee.fetch_reference(get=without)


def test_a_missing_age_group_fails_the_refresh_instead_of_skewing_the_brackets():
    import pytest

    def without(url: str) -> bytes:
        payload = _fake_payload(url)
        payload["observations"] = [
            o for o in payload["observations"] if o["dimensions"]["AGE"] != "Y20T24"
        ]
        return json.dumps(payload).encode()

    with pytest.raises(insee.InseeFetchError, match="Y20T24"):
        insee.fetch_reference(get=without)


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
