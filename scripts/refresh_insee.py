"""Refresh the cached INSEE reference shares from the Melodi API.

Run by the `insee-refresh` service, or by hand. On a fetch error it exits with
an error and keeps the existing values. Safe to rerun.

    python -m scripts.refresh_insee
"""

from __future__ import annotations

import sys

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.insee import InseeFetchError, fetch_reference
from app.models import InseeReference


def main() -> int:
    try:
        data = fetch_reference()
    except InseeFetchError as exc:
        print(f"INSEE refresh skipped, fetch failed: {exc}", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        for dimension, mapping in data.values.items():
            # Replace this dimension's rows (same transaction).
            db.execute(delete(InseeReference).where(InseeReference.dimension == dimension))
            for category, pct in mapping.items():
                db.add(
                    InseeReference(
                        dimension=dimension,
                        category=category,
                        percentage=pct,
                        source_year=data.source_year,
                    )
                )
        db.commit()
    except SQLAlchemyError as exc:
        # A message and an exit code rather than a traceback.
        db.rollback()
        print(f"INSEE refresh skipped, database error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    dims = ", ".join(f"{d} ({len(m)})" for d, m in data.values.items())
    print(f"INSEE reference updated for {data.source_year}: {dims}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
