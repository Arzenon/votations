#!/bin/sh
# Monthly INSEE refresh (estimates are published yearly). Retries with backoff,
# see docker/periodic.sh.
set -e

. "$(dirname "$0")/periodic.sh"

# Touched after each successful run; the healthcheck watches its age.
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

run_periodically \
	"${INSEE_REFRESH_INTERVAL:-2592000}" \
	"${INSEE_RETRY_INTERVAL:-30}" \
	"rafraîchissement INSEE" \
	python -m scripts.refresh_insee
