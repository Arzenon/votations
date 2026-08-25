"""Refresh cached INSEE reference shares from the open Melodi API.

Run manually or on a schedule (see the `insee-refresh` service in
docker-compose). On any fetch error it exits non-zero **without touching** the
existing cached values, so the app keeps serving the last good data (or the
curated fallback). Safe to run repeatedly.

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
        print(f"INSEE refresh skipped — fetch failed: {exc}", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        for dimension, mapping in data.values.items():
            # Replace this dimension's rows atomically with the fresh snapshot.
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
        # Any database refusal, not only "the schema is not migrated yet": the
        # containerised loop reads a traceback as a crash and retries in seconds.
        db.rollback()
        print(f"INSEE refresh skipped — database: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    dims = ", ".join(f"{d} ({len(m)})" for d, m in data.values.items())
    print(f"INSEE reference updated for {data.source_year}: {dims}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
