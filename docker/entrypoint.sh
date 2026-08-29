#!/bin/sh
# Apply migrations (and optionally seed sample data) before starting the app.
# The db service is gated by a healthcheck in docker-compose, so it is ready here.
#
# This assumes **one app container**, which is what docker-compose.prod.yml runs.
# Two starting together would both call `alembic upgrade head` on the same
# database: Alembic takes no lock of its own, so they race, and the loser fails
# on a revision the winner has already applied — the container then restarts and
# succeeds, but the first boot looks like a crash for no reason anybody can see.
#
# To scale the app, run the migration once as its own step (a `docker compose run
# --rm app alembic upgrade head` before `up`) and drop it from here. The same
# single-container caveat applies to releasing interrupted AI drafts at boot —
# see `app.main._release_interrupted_drafts`.
set -e

echo "→ Applying database migrations…"
alembic upgrade head

if [ "${SEED_ON_START:-0}" = "1" ]; then
  echo "→ Seeding sample laws…"
  python -m scripts.seed
fi

echo "→ Starting: $*"
exec "$@"
