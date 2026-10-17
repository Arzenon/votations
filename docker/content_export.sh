#!/bin/sh
# Daily export of the editorial content (laws, revisions, import decisions).
# Not the backup (see scripts/backup.sh); used to move or seed an instance.
set -e

. "$(dirname "$0")/periodic.sh"

HEARTBEAT="${HEARTBEAT_FILE:-/tmp/export-alive}"
EXPORT_DIR="${CONTENT_EXPORT_DIR:-/exports}"
KEEP_DAYS="${CONTENT_KEEP_DAYS:-5}"

run_periodically \
	"${CONTENT_EXPORT_INTERVAL:-86400}" \
	"${CONTENT_RETRY_INTERVAL:-60}" \
	"export du contenu" \
	python -m scripts.content export --dir "$EXPORT_DIR" --keep-days "$KEEP_DAYS"
