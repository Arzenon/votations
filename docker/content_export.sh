#!/bin/sh
# Daily snapshot of the editorial content: the laws, their public record of
# changes, and the review decisions taken on imported ones.
#
# Not the backup — `scripts/backup.sh` dumps the whole database and is what
# protects the votes. This is the portable half, for moving an instance to
# another host or seeding a fresh one without rewriting the presentations by
# hand. Keeping a few days of snapshots also means a presentation somebody
# mangled can be read back out of yesterday's file.
set -e

HEARTBEAT="${HEARTBEAT_FILE:-/tmp/export-alive}"
EXPORT_DIR="${CONTENT_EXPORT_DIR:-/exports}"
KEEP_DAYS="${CONTENT_KEEP_DAYS:-5}"

LONG="${CONTENT_EXPORT_INTERVAL:-86400}"   # 24 hours
SHORT="${CONTENT_RETRY_INTERVAL:-60}"      # seconds, before the first success

while true; do
  if python -m scripts.content export --dir "$EXPORT_DIR" --keep-days "$KEEP_DAYS"; then
    : > "$HEARTBEAT"
    sleep "$LONG"
  else
    echo "→ export du contenu pas encore possible ; nouvel essai dans ${SHORT}s"
    sleep "$SHORT"
  fi
done
