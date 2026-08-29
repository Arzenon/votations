"""French display labels for values stored in English or unaccented form.

The database keeps stable, ASCII, machine-readable states (``open``, ``importe``)
because they end up in URLs, CSS class names and the open-data export. Everything
a visitor reads goes through here instead, so no English enum value ever reaches
a French page. The same goes for dates: a promulgation is cited in French prose,
never as an ISO string.
"""

from datetime import UTC, date, datetime

from app.config import display_zone
from app.models.imported_law import (
    STATUS_IGNORED,
    STATUS_IMPORTED,
    STATUS_NEW,
)
from app.models.law import STATUS_CLOSED, STATUS_DRAFT, STATUS_OPEN, STATUS_SCHEDULED

#: Headings for the home page's bands. Distinct from LAW_STATUS_LABELS on
#: purpose: that one labels a single votation's badge (« ouverte »), this one
#: names a section of the page (« Votations ouvertes »), and the two want
#: different words.
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

# Column names as a reader should see them, for the public record of changes made
# to a law after publication.
FIELD_LABELS = {
    "title": "Titre",
    "official_ref": "Référence officielle",
    "legifrance_url": "Lien Légifrance",
    "promulgated_on": "Date de promulgation",
    "summary_text": "Ce que dit la loi",
    "practical_changes": "Ce que ça change en pratique",
    "arguments_for": "Arguments des partisans",
    "arguments_against": "Arguments des opposants",
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


def _local_day(value: date) -> date:
    """The calendar day ``value`` falls on, as a reader here would name it.

    A plain :class:`~datetime.date` carries no zone and is returned unchanged — a
    promulgation happened on a day, not at an instant.

    A :class:`~datetime.datetime` is an instant, and every one of them in this
    application comes out of a ``timestamptz`` column in UTC. Reading ``.day`` off
    it filed anything between midnight and 2 a.m. Paris under the previous day,
    which is how a vote scheduled to close on the 15th announced the 14th on the
    home page. ``datetime`` subclasses ``date``, so this had to be checked for
    rather than caught by the type.
    """
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(display_zone()).date()
    return value


def french_date(value: date) -> str:
    """e.g. ``24 juin 2026`` — the form a law is cited in.

    Accepts an instant as well as a date, and converts it first; see
    :func:`_local_day`.
    """
    day = _local_day(value)
    return f"{day.day} {MOIS[day.month - 1]} {day.year}"


def french_datetime(value: datetime) -> str:
    """e.g. ``20/08/2026 à 19:50 (heure de Paris)``.

    Used where an instant is shown to a reader rather than to a machine — the
    public record of changes, in particular. An ISO string with microseconds is
    unambiguous and unreadable; this is unambiguous and readable, which is what a
    transparency page needs.
    """
    zone = display_zone()
    local = value.astimezone(zone) if value.tzinfo else value.replace(tzinfo=UTC).astimezone(zone)
    return f"{local:%d/%m/%Y à %H:%M} ({zone.key})"
