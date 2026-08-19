"""French display labels for values stored in English or unaccented form.

The database keeps stable, ASCII, machine-readable states (``open``, ``importe``)
because they end up in URLs, CSS class names and the open-data export. Everything
a visitor reads goes through here instead, so no English enum value ever reaches
a French page.
"""

from app.models.imported_law import (
    STATUS_IGNORED,
    STATUS_IMPORTED,
    STATUS_NEW,
)
from app.models.law import STATUS_CLOSED, STATUS_DRAFT, STATUS_OPEN, STATUS_SCHEDULED

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


def law_status(value: str) -> str:
    return LAW_STATUS_LABELS.get(value, value)


def import_status(value: str) -> str:
    return IMPORT_STATUS_LABELS.get(value, value)
