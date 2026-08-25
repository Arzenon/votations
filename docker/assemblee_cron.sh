#!/bin/sh
# Daily import of newly promulgated laws (containerised "cron").
# The Assemblée nationale rebuilds its export once a day, so a 24 h cadence
# matches the source exactly. Retries quickly at first (the usual failure is the
# app still applying migrations), then backs off — see docker/periodic.sh.
set -e

. "$(dirname "$0")/periodic.sh"

# Touched after each successful run; the compose healthcheck watches its age, so
# a job that has quietly stopped succeeding shows up as unhealthy instead of as
# a container that is still "running".
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

run_periodically \
	"${AN_REFRESH_INTERVAL:-86400}" \
	"${AN_RETRY_INTERVAL:-60}" \
	"import AN" \
	python -m scripts.refresh_assemblee
