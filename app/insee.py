"""Fetch national reference shares from the open INSEE Melodi API.

Produces the ``sexe`` and ``region`` reference distributions from the
``DS_ESTIMATION_POPULATION`` dataset (population au 1ᵉʳ janvier, by sex and by
region). The dataset's age dimension only carries coarse groups (25-59, ≥60, …),
which cannot be mapped to the app's brackets, so **age is intentionally not
fetched** and keeps its curated fallback.

Design notes:
- The HTTP getter is injectable so the parsing/mapping is unit-testable offline.
- Any failure raises :class:`InseeFetchError`; callers leave cached values intact.
- The geography référentiel year prefix (e.g. ``2026-REG-11``) is discovered at
  runtime so the job keeps working after the yearly rollover.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.models import DIMENSION_AGE, DIMENSION_REGION, DIMENSION_SEXE

# App age brackets as (weighted) sums of INSEE quinquennal groups, restricted to
# adults. Only 18-24 is approximate: ages 18-19 are taken as 2/5 of the 15-19
# group (uniform-within-band assumption). All other brackets are exact unions.
AGE_BRACKET_COMPOSITION = {
    "18-24": [("Y15T19", 0.4), ("Y20T24", 1.0)],
    "25-34": [("Y25T29", 1.0), ("Y30T34", 1.0)],
    "35-49": [("Y35T39", 1.0), ("Y40T44", 1.0), ("Y45T49", 1.0)],
    "50-64": [("Y50T54", 1.0), ("Y55T59", 1.0), ("Y60T64", 1.0)],
    "65+": [
        ("Y65T69", 1.0),
        ("Y70T74", 1.0),
        ("Y75T79", 1.0),
        ("Y80T84", 1.0),
        ("Y85T89", 1.0),
        ("Y90T94", 1.0),
        ("Y_GE95", 1.0),
    ],
}

# INSEE région code → app Region label. Overseas (01,02,03,04,06) fold into "Outre-mer".
REGION_CODE_TO_LABEL = {
    "11": "Île-de-France",
    "24": "Centre-Val de Loire",
    "27": "Bourgogne-Franche-Comté",
    "28": "Normandie",
    "32": "Hauts-de-France",
    "44": "Grand Est",
    "52": "Pays de la Loire",
    "53": "Bretagne",
    "75": "Nouvelle-Aquitaine",
    "76": "Occitanie",
    "84": "Auvergne-Rhône-Alpes",
    "93": "Provence-Alpes-Côte d'Azur",
    "94": "Corse",
}
OVERSEAS_CODES = ("01", "02", "03", "04", "06")
OVERSEAS_LABEL = "Outre-mer"


class InseeFetchError(RuntimeError):
    """Raised on any network/HTTP/parse problem so callers can keep old values."""


@dataclass
class InseeData:
    source_year: int
    values: dict[str, dict[str, float]]  # {"sexe": {...}, "region": {...}}


Getter = Callable[[str], dict]


def _default_getter(url: str) -> dict:
    settings = get_settings()
    headers = {"Accept": "application/json"}
    if settings.insee_api_key:
        headers["Authorization"] = f"Bearer {settings.insee_api_key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise InseeFetchError(str(exc)) from exc


def _data_url(geo: str, **filters: str) -> str:
    settings = get_settings()
    params = {"GEO": geo, **filters}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{settings.insee_melodi_base}/data/{settings.insee_dataset}?{query}"


def _observations(payload: dict) -> list[dict]:
    obs = payload.get("observations") or []
    if not obs:
        raise InseeFetchError("empty observations")
    return obs


def _latest(obs: list[dict], pick: Callable[[dict], bool]) -> tuple[int, list[dict]]:
    """Return the most recent TIME_PERIOD and the matching observations."""
    filtered = [o for o in obs if pick(o)]
    if not filtered:
        raise InseeFetchError("no matching observations")
    year = max(int(o["dimensions"]["TIME_PERIOD"]) for o in filtered)
    return year, [o for o in filtered if int(o["dimensions"]["TIME_PERIOD"]) == year]


def _value(o: dict) -> float:
    return float(o["measures"]["OBS_VALUE_NIVEAU"]["value"])


def _discover_prefix(get: Getter) -> str:
    """Find the current geography référentiel year prefix (e.g. '2026')."""
    current = datetime.now(UTC).year
    for prefix in (current, current + 1, current - 1, current - 2):
        url = _data_url(f"{prefix}-FRANCE-F", AGE="_T", EP_MEASURE="POP_JAN_1ST")
        try:
            payload = get(url)
            if payload.get("observations"):
                return str(prefix)
        except InseeFetchError:
            continue
    raise InseeFetchError("could not determine current INSEE geo référentiel")


def fetch_reference(get: Getter | None = None) -> InseeData:
    """Fetch sexe + region reference shares. Raises InseeFetchError on failure."""
    get = get or _default_getter
    prefix = _discover_prefix(get)

    # --- Sexe: counts for France (all), AGE total, by sex, latest year. ---
    fr = _observations(get(_data_url(f"{prefix}-FRANCE-F", AGE="_T", EP_MEASURE="POP_JAN_1ST")))
    sex_year, sex_obs = _latest(fr, lambda o: o["dimensions"]["SEX"] in ("F", "M"))
    by_sex = {o["dimensions"]["SEX"]: _value(o) for o in sex_obs}
    total_sex = by_sex.get("F", 0) + by_sex.get("M", 0)
    if total_sex <= 0:
        raise InseeFetchError("sex totals missing")
    sexe = {
        "femme": round(100 * by_sex["F"] / total_sex, 1),
        "homme": round(100 * by_sex["M"] / total_sex, 1),
    }

    # --- Age: quinquennal counts (France, total sex) → app brackets, adults only. ---
    age_obs = _observations(
        get(_data_url(f"{prefix}-FRANCE-F", SEX="_T", EP_MEASURE="POP_JAN_1ST"))
    )
    age_year, age = _age_reference(age_obs)

    # --- Region: total population per région, latest year, normalised to 100. ---
    region_pop: dict[str, float] = {}
    overseas = 0.0
    region_year = sex_year
    for code in list(REGION_CODE_TO_LABEL) + list(OVERSEAS_CODES):
        url = _data_url(f"{prefix}-REG-{code}", AGE="_T", SEX="_T", EP_MEASURE="POP_JAN_1ST")
        try:
            year, obs = _latest(_observations(get(url)), lambda o: True)
        except InseeFetchError:
            continue  # a missing région must not abort the whole refresh
        region_year = max(region_year, year)
        if code in OVERSEAS_CODES:
            overseas += _value(obs[0])
        else:
            region_pop[REGION_CODE_TO_LABEL[code]] = _value(obs[0])
    if overseas:
        region_pop[OVERSEAS_LABEL] = overseas
    if not region_pop:
        raise InseeFetchError("no region data")

    grand_total = sum(region_pop.values())
    region = {label: round(100 * pop / grand_total, 1) for label, pop in region_pop.items()}

    return InseeData(
        source_year=max(sex_year, age_year, region_year),
        values={DIMENSION_AGE: age, DIMENSION_SEXE: sexe, DIMENSION_REGION: region},
    )


def _age_reference(obs: list[dict]) -> tuple[int, dict[str, float]]:
    """Aggregate quinquennal counts into the app's adult brackets, normalised."""
    year, rows = _latest(obs, lambda o: o["dimensions"]["AGE"] != "_T")
    by_age = {o["dimensions"]["AGE"]: _value(o) for o in rows}
    brackets = {
        label: sum(by_age.get(code, 0.0) * w for code, w in comp)
        for label, comp in AGE_BRACKET_COMPOSITION.items()
    }
    adult_total = sum(brackets.values())
    if adult_total <= 0:
        raise InseeFetchError("no age data")
    return year, {label: round(100 * v / adult_total, 1) for label, v in brackets.items()}
