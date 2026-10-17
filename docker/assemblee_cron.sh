#!/bin/sh
# Daily import of newly promulgated laws (the export is rebuilt daily).
# Retries with backoff, see docker/periodic.sh.
set -e

. "$(dirname "$0")/periodic.sh"

# Touched after each successful run; the healthcheck watches its age.
HEARTBEAT="${HEARTBEAT_FILE:-/tmp/refresh-alive}"

run_periodically \
	"${AN_REFRESH_INTERVAL:-86400}" \
	"${AN_RETRY_INTERVAL:-60}" \
	"import AN" \
	python -m scripts.refresh_assemblee
