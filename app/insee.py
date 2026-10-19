"""National reference shares (age, sex, region) from the INSEE Melodi API.

Uses ``DS_ESTIMATION_POPULATION`` (population on 1 January). INSEE publishes
five-year age groups, summed into our brackets by `AGE_BRACKET_COMPOSITION`
and normalised over adults only. Failures raise `InseeFetchError` and cached
values are kept. The geography year prefix (e.g. ``2026-REG-11``) is detected
at runtime.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings
from app.models import DIMENSION_AGE, DIMENSION_REGION, DIMENSION_SEXE
from app.opendata import Getter, getter

# Our age brackets as weighted sums of INSEE five-year groups. Only 18-24 is
# approximate: 18-19 year olds are taken as 2/5 of the 15-19 group.
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

# INSEE region code to our label. Overseas (01-04, 06) are grouped as "Outre-mer".
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
    """Network or parse failure. Callers keep the cached values."""


@dataclass
class InseeData:
    source_year: int
    values: dict[str, dict[str, float]]  # {"age": {...}, "sexe": {...}, "region": {...}}


def _headers() -> dict[str, str]:
    """Request headers. The API key is optional."""
    settings = get_settings()
    headers = {"Accept": "application/json"}
    if settings.insee_api_key:
        headers["Authorization"] = f"Bearer {settings.insee_api_key}"
    return headers


# Responses are a few kB.
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_default_getter = getter(
    timeout=30.0, max_bytes=MAX_PAYLOAD_BYTES, error=InseeFetchError, headers=_headers
)


def _payload(get: Getter, url: str) -> dict:
    """Fetch and decode one Melodi response."""
    try:
        return json.loads(get(url))
    except ValueError as exc:
        raise InseeFetchError(f"réponse illisible : {exc}") from exc


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


def _dimension(o: object, name: str) -> str:
    """An observation's value for a dimension, or "".

    Tolerates shape changes so they surface as InseeFetchError, not KeyError.
    """
    dimensions = o.get("dimensions") if isinstance(o, dict) else None
    value = dimensions.get(name) if isinstance(dimensions, dict) else None
    return "" if value is None else str(value)


def _year(o: dict) -> int:
    """An observation's TIME_PERIOD as an int."""
    raw = _dimension(o, "TIME_PERIOD")
    try:
        return int(raw)
    except ValueError as exc:
        raise InseeFetchError(f"TIME_PERIOD illisible : « {raw} »") from exc


def _latest(obs: list[dict], pick: Callable[[dict], bool]) -> tuple[int, list[dict]]:
    """Return the most recent TIME_PERIOD and the matching observations."""
    filtered = [o for o in obs if pick(o)]
    if not filtered:
        raise InseeFetchError("no matching observations")
    year = max(_year(o) for o in filtered)
    return year, [o for o in filtered if _year(o) == year]


def _value(o: dict) -> float:
    """An observation's value. Raises InseeFetchError if missing."""
    try:
        return float(o["measures"]["OBS_VALUE_NIVEAU"]["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InseeFetchError(f"observation sans valeur exploitable : {exc}") from exc


def _discover_prefix(get: Getter) -> str:
    """Find the current geography référentiel year prefix (e.g. '2026')."""
    current = datetime.now(UTC).year
    for prefix in (current, current + 1, current - 1, current - 2):
        url = _data_url(f"{prefix}-FRANCE-F", AGE="_T", EP_MEASURE="POP_JAN_1ST")
        try:
            payload = _payload(get, url)
            if payload.get("observations"):
                return str(prefix)
        except InseeFetchError:
            continue
    raise InseeFetchError("could not determine current INSEE geo référentiel")


def fetch_reference(get: Getter | None = None) -> InseeData:
    """Fetch age, sex and region shares. Raises InseeFetchError on failure."""
    get = get or _default_getter
    prefix = _discover_prefix(get)

    # --- Sexe: counts for France (all), AGE total, by sex, latest year. ---
    fr = _observations(
        _payload(get, _data_url(f"{prefix}-FRANCE-F", AGE="_T", EP_MEASURE="POP_JAN_1ST"))
    )
    sex_year, sex_obs = _latest(fr, lambda o: _dimension(o, "SEX") in ("F", "M"))
    by_sex = {_dimension(o, "SEX"): _value(o) for o in sex_obs}
    total_sex = by_sex.get("F", 0) + by_sex.get("M", 0)
    if total_sex <= 0:
        raise InseeFetchError("sex totals missing")
    sexe = {
        "femme": round(100 * by_sex["F"] / total_sex, 1),
        "homme": round(100 * by_sex["M"] / total_sex, 1),
    }

    # --- Age: five-year groups (France, both sexes) into our adult brackets. ---
    age_obs = _observations(
        _payload(get, _data_url(f"{prefix}-FRANCE-F", SEX="_T", EP_MEASURE="POP_JAN_1ST"))
    )
    age_year, age = _age_reference(age_obs)

    # --- Region: total population per région, latest year, normalised to 100. ---
    region_pop: dict[str, float] = {}
    overseas = 0.0
    region_year = sex_year
    for code in list(REGION_CODE_TO_LABEL) + list(OVERSEAS_CODES):
        url = _data_url(f"{prefix}-REG-{code}", AGE="_T", SEX="_T", EP_MEASURE="POP_JAN_1ST")
        try:
            year, obs = _latest(_observations(_payload(get, url)), lambda o: True)
            population = _value(obs[0])
        except InseeFetchError as exc:
            # A missing région would inflate every other share once normalised,
            # and replace the cached values. Only an overseas code may be absent.
            if code not in OVERSEAS_CODES:
                raise InseeFetchError(f"région {code} indisponible : {exc}") from exc
            continue
        region_year = max(region_year, year)
        if code in OVERSEAS_CODES:
            overseas += population
        else:
            region_pop[REGION_CODE_TO_LABEL[code]] = population
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
    """Sum five-year groups into our adult brackets, as percentages."""
    # Exclude the total row ("_T") and rows without an AGE ("").
    year, rows = _latest(obs, lambda o: _dimension(o, "AGE") not in ("_T", ""))
    by_age = {_dimension(o, "AGE"): _value(o) for o in rows}
    brackets = {
        label: sum(by_age.get(code, 0.0) * w for code, w in comp)
        for label, comp in AGE_BRACKET_COMPOSITION.items()
    }
    adult_total = sum(brackets.values())
    if adult_total <= 0:
        raise InseeFetchError("no age data")
    return year, {label: round(100 * v / adult_total, 1) for label, v in brackets.items()}
