#!/bin/sh
# Periodic INSEE reference refresh (containerised "cron").
# INSEE publishes population estimates once a year (1 January), so a monthly
# cadence is ample. Retries quickly at first (the usual failure is the app still
# applying migrations), then backs off — see docker/periodic.sh.
set -e

. "$(dirname "$0")/periodic.sh"

# Touched after each successful run; the compose healthcheck watches its age, so
# a job that has quietly stopped succeeding shows up as unhealthy instead of as
# a container that is still "running".
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

run_periodically \
	"${INSEE_REFRESH_INTERVAL:-2592000}" \
	"${INSEE_RETRY_INTERVAL:-30}" \
	"rafraîchissement INSEE" \
	python -m scripts.refresh_insee
