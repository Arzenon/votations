#!/bin/sh
# Run migrations (and optionally seed sample data), then start the app.
#
# Assumes a single app container: two would run the migrations concurrently.
# To scale, run `docker compose run --rm app alembic upgrade head` before `up`
# and remove it from here (see also `app.main._release_interrupted_drafts`).
set -e

echo "→ Applying database migrations…"
alembic upgrade head

if [ "${SEED_ON_START:-0}" = "1" ]; then
  echo "→ Seeding sample laws…"
  python -m scripts.seed
fi

echo "→ Starting: $*"
exec "$@"
