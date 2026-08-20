#!/bin/sh
# Daily import of newly promulgated laws (containerised "cron").
# The Assemblée nationale rebuilds its export once a day, so a 24 h cadence
# matches the source exactly. Retries quickly until the first success (e.g.
# while the app is still applying migrations), then settles into the long
# interval.
set -e

# Touched after each successful run; the compose healthcheck watches its age, so
# a job that has quietly stopped succeeding shows up as unhealthy instead of as
# a container that is still "running".
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

LONG="${AN_REFRESH_INTERVAL:-86400}"    # 24 hours
SHORT="${AN_RETRY_INTERVAL:-60}"        # seconds, before first success

while true; do
  if python -m scripts.refresh_assemblee; then
    : > "$HEARTBEAT"
    sleep "$LONG"
  else
    echo "→ import AN pas encore appliqué ; nouvel essai dans ${SHORT}s"
    sleep "$SHORT"
  fi
done
