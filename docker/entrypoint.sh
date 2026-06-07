#!/bin/sh
# Apply migrations (and optionally seed sample data) before starting the app.
# The db service is gated by a healthcheck in docker-compose, so it is ready here.
set -e

echo "→ Applying database migrations…"
alembic upgrade head

if [ "${SEED_ON_START:-0}" = "1" ]; then
  echo "→ Seeding sample laws…"
  python -m scripts.seed
fi

echo "→ Starting: $*"
exec "$@"
