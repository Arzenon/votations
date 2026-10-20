"""French labels for values stored in English or without accents, and dates.

Stored states stay ASCII since they appear in URLs, CSS classes and exports.
"""

from datetime import date, datetime

from app.config import display_zone
from app.db import as_utc
from app.models.imported_law import (
    STATUS_IGNORED,
    STATUS_IMPORTED,
    STATUS_NEW,
)
from app.models.law import STATUS_CLOSED, STATUS_DRAFT, STATUS_OPEN, STATUS_SCHEDULED
from app.models.vote import VoteChoice

# In ballot order; templates iterate over it.
CHOICE_LABELS = {
    VoteChoice.FOR.value: "Pour",
    VoteChoice.AGAINST.value: "Contre",
    VoteChoice.ABSTAIN.value: "Abstention",
}

# Home page section headings.
HOME_BAND_LABELS = {
    STATUS_OPEN: "Votations ouvertes",
    STATUS_SCHEDULED: "Bientôt ouvertes",
    STATUS_CLOSED: "Votations closes",
}

LAW_STATUS_LABELS = {
    STATUS_DRAFT: "brouillon",
    STATUS_SCHEDULED: "programmée",
    STATUS_OPEN: "ouverte",
    STATUS_CLOSED: "close",
}

IMPORT_STATUS_LABELS = {
    STATUS_NEW: "nouvelle",
    STATUS_IMPORTED: "importée",
    STATUS_IGNORED: "ignorée",
}

# Field names for the public revision log.
FIELD_LABELS = {
    "title": "Titre",
    "official_ref": "Référence officielle",
    "legifrance_url": "Lien Légifrance",
    "promulgated_on": "Date de promulgation",
    "summary_text": "Ce que dit la loi",
    "practical_changes": "Ce que ça change en pratique",
    "arguments_for": "Arguments des partisans",
    "arguments_against": "Arguments des opposants",
    "debate_summary": "Ce qui a été débattu",
    "opens_at": "Ouverture du vote",
    "closes_at": "Clôture du vote",
    "published": "Publication",
}

MOIS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def law_status(value: str) -> str:
    return LAW_STATUS_LABELS.get(value, value)


def home_band(value: str) -> str:
    return HOME_BAND_LABELS.get(value, value)


def import_status(value: str) -> str:
    return IMPORT_STATUS_LABELS.get(value, value)


def field_label(value: str) -> str:
    return FIELD_LABELS.get(value, value)


def local_day(value: date) -> date:
    """The local calendar day of ``value``.

    A datetime is converted to the display zone first (otherwise 00:30 Paris
    would land on the previous day). A plain date is returned as is.
    """
    if isinstance(value, datetime):
        return as_utc(value).astimezone(display_zone()).date()
    return value


def french_date(value: date) -> str:
    """e.g. ``24 juin 2026``. Accepts a date or a datetime."""
    day = local_day(value)
    return f"{day.day} {MOIS[day.month - 1]} {day.year}"


def french_datetime(value: datetime) -> str:
    """e.g. ``20/08/2026 à 19:50 (Europe/Paris)``."""
    zone = display_zone()
    local = as_utc(value).astimezone(zone)
    return f"{local:%d/%m/%Y à %H:%M} ({zone.key})"
