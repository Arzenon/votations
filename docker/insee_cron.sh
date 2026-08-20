#!/bin/sh
# Periodic INSEE reference refresh (containerised "cron").
# INSEE publishes population estimates once a year (1 January), so a monthly
# cadence is ample. Retries quickly until the first success (e.g. while the app
# is still applying migrations), then settles into the long interval.
set -e

# Touched after each successful run; the compose healthcheck watches its age, so
# a job that has quietly stopped succeeding shows up as unhealthy instead of as
# a container that is still "running".
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

LONG="${INSEE_REFRESH_INTERVAL:-2592000}"   # 30 days
SHORT="${INSEE_RETRY_INTERVAL:-30}"          # seconds, before first success

while true; do
  if python -m scripts.refresh_insee; then
    : > "$HEARTBEAT"
    sleep "$LONG"
  else
    echo "→ INSEE refresh not applied yet; retrying in ${SHORT}s"
    sleep "$SHORT"
  fi
done
