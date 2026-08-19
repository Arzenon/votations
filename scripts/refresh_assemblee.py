"""Import newly promulgated laws from the Assemblée nationale open data.

Run manually or on a schedule (see the `assemblee-refresh` service in
docker-compose). On any fetch error it exits non-zero **without touching** the
staged rows, so the admin keeps seeing the last good list. Safe to run
repeatedly: rows are keyed on the dossier identifier, and an administrator's
decision on a row is never overwritten.

    python -m scripts.refresh_assemblee
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import OperationalError, ProgrammingError

from app.assemblee import AssembleeFetchError, fetch_promulgated_laws
from app.crud import imports as imports_crud
from app.db import SessionLocal


def main() -> int:
    try:
        records = fetch_promulgated_laws()
    except AssembleeFetchError as exc:
        print(f"Import AN ignoré — échec de récupération : {exc}", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        created, refreshed = imports_crud.sync(db, records=records)
    except (OperationalError, ProgrammingError) as exc:
        db.rollback()
        print(f"Import AN ignoré — schéma non prêt : {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"Lois promulguées : {created} nouvelle(s), {refreshed} actualisée(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
