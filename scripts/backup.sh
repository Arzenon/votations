#!/bin/sh
# Dump / restore the Votations database.
#
#   backup.sh dump              one dump into $BACKUP_DIR
#   backup.sh loop              a dump every $BACKUP_INTERVAL (the container)
#   backup.sh list              list the dumps
#   backup.sh restore <file>    restore a dump (destructive, asks first)
#
# Connection settings come from the PG* environment variables. Dumps stay on
# this host: also copy them elsewhere (see DEPLOY.md).
set -e

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL:-86400}"
# Minimum free space (MB) to start a dump: filling the disk would also bring
# down Postgres. See « Espace disque » in DEPLOY.md.
MIN_FREE_MB="${BACKUP_MIN_FREE_MB:-500}"
HEARTBEAT="${BACKUP_HEARTBEAT:-/tmp/backup-alive}"

# Default signal handling: exit. `dump` sets its own trap during a dump and
# calls this again afterwards.
_idle_traps() {
  trap 'exit 0' HUP INT TERM
}

# Sleep in the background so SIGTERM is handled (sh is PID 1). Same as
# docker/periodic.sh, which can't be sourced from the Postgres image.
_nap() {
  sleep "$1" &
  wait $!
}

free_mb() {
  df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'
}

# Remove what an interrupted run left behind (raw dump, stderr file, partial
# archive). The raw dump is the largest file and the retention sweep doesn't
# match it.
clean_partials() {
  # Older than an hour, so a running dump isn't affected.
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name '.votations-*.sql' -o -name '.votations-*.sql.err' \
       -o -name 'votations-*.sql.gz.partial' \) \
    -mmin +60 -print -delete 2>/dev/null || true
}

# A separate function so the caller can clean up on every path (an early
# return doesn't fire the EXIT trap).
_write_dump() {
  free=$(free_mb "$BACKUP_DIR")
  if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_MB" ]; then
    echo "✗ espace insuffisant : ${free} Mo libres, minimum ${MIN_FREE_MB} Mo." >&2
    echo "  Libérez de l'espace ou copiez les sauvegardes hors de la machine." >&2
    return 1
  fi

  # Dump to a file rather than a pipe: `pg_dump | gzip` would return gzip's
  # exit status and hide a pg_dump failure.
  if ! pg_dump --no-owner --no-privileges > "$raw" 2>"$raw.err"; then
    echo "✗ pg_dump a échoué : $(head -1 "$raw.err" 2>/dev/null)" >&2
    return 1
  fi

  # Check the votes table is in the dump.
  if ! grep -q "CREATE TABLE" "$raw" || ! grep -q "votes" "$raw"; then
    echo "✗ sauvegarde incomplète : ni schéma ni table votes — rejetée" >&2
    return 1
  fi

  gzip -c "$raw" > "$target.partial"
  mv "$target.partial" "$target"
  echo "→ sauvegarde : $target ($(wc -c < "$target") octets)"

  find "$BACKUP_DIR" -name 'votations-*.sql.gz' -type f -mtime "+$KEEP_DAYS" -print -delete

  # Only touched after a verified dump.
  : > "$HEARTBEAT"

  remaining=$(free_mb "$BACKUP_DIR")
  if [ -n "$remaining" ] && [ "$remaining" -lt $((MIN_FREE_MB * 2)) ]; then
    echo "⚠ espace faible : ${remaining} Mo libres après sauvegarde." >&2
  fi
}

dump() {
  mkdir -p "$BACKUP_DIR"
  clean_partials

  stamp=$(date -u +%Y%m%d-%H%M%S)
  target="$BACKUP_DIR/votations-$stamp.sql.gz"
  raw="$BACKUP_DIR/.votations-$stamp.sql"

  # The trap handles abrupt exits (SIGTERM during a dump). Normal paths are
  # cleaned up by the lines after the call.
  trap 'rm -f "$raw" "$raw.err" "$target.partial"; exit 1' HUP INT TERM

  if _write_dump; then status=0; else status=$?; fi

  rm -f "$raw" "$raw.err" "$target.partial"
  _idle_traps
  return "$status"
}

case "${1:-dump}" in
  dump)
    dump
    ;;
  loop)
    echo "→ sauvegarde toutes les ${INTERVAL}s, conservation ${KEEP_DAYS} jours"
    _idle_traps
    while true; do
      dump || echo "→ nouvel essai au prochain cycle"
      _nap "$INTERVAL"
    done
    ;;
  list)
    ls -lh "$BACKUP_DIR"/votations-*.sql.gz 2>/dev/null || echo "aucune sauvegarde"
    ;;
  restore)
    file="$2"
    [ -n "$file" ] || { echo "usage: backup.sh restore <fichier.sql.gz>" >&2; exit 2; }
    [ -f "$file" ] || { echo "introuvable : $file" >&2; exit 2; }

    # Check the archive before asking and before dropping anything.
    if ! gunzip -t "$file" 2>/dev/null; then
      echo "✗ archive illisible ou tronquée : $file" >&2
      echo "  Rien n'a été touché. Essayez une sauvegarde plus ancienne." >&2
      exit 1
    fi

    echo "⚠️  Ceci écrase la base ${PGDATABASE:-votations} sur ${PGHOST:-localhost}."
    printf "Taper 'oui' pour confirmer : "
    read -r answer
    [ "$answer" = "oui" ] || { echo "annulé"; exit 1; }

    # Empty the schema first, otherwise the restore fails halfway on "relation
    # already exists".
    psql --quiet --set ON_ERROR_STOP=1 \
      -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
    gunzip -c "$file" | psql --quiet --set ON_ERROR_STOP=1
    echo "→ restauration terminée depuis $file"
    ;;
  *)
    echo "usage: backup.sh [dump|loop|list|restore <fichier>]" >&2
    exit 2
    ;;
esac
