#!/bin/sh
# Daily import of newly promulgated laws (containerised "cron").
# The Assemblée nationale rebuilds its export once a day, so a 24 h cadence
# matches the source exactly. Retries quickly until the first success (e.g.
# while the app is still applying migrations), then settles into the long
# interval.
set -e

LONG="${AN_REFRESH_INTERVAL:-86400}"    # 24 hours
SHORT="${AN_RETRY_INTERVAL:-60}"        # seconds, before first success

while true; do
  if python -m scripts.refresh_assemblee; then
    sleep "$LONG"
  else
    echo "→ import AN pas encore appliqué ; nouvel essai dans ${SHORT}s"
    sleep "$SHORT"
  fi
done
