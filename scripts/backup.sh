#!/bin/sh
# Dump / restore the Votations database.
#
#   backup.sh dump              one dump into $BACKUP_DIR
#   backup.sh loop              a dump every $BACKUP_INTERVAL (the container)
#   backup.sh list              what is on disk
#   backup.sh restore <file>    load a dump back (destructive — asks first)
#
# Connection settings come from the standard PG* environment variables, so no
# credentials are written here. Dumps stay on this host: that protects against
# breaking the data, not against losing the machine. Copy them off-host too —
# see DEPLOY.md.
set -e

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL:-86400}"
# Refuse to start a dump with less than this free (MB). A dump that fills the
# disk does not just fail: it takes the database down with it, and Postgres is on
# the same host. Archived votations are kept for ever now, so the data only ever
# grows — see « Espace disque » dans DEPLOY.md.
MIN_FREE_MB="${BACKUP_MIN_FREE_MB:-500}"
HEARTBEAT="${BACKUP_HEARTBEAT:-/tmp/backup-alive}"

# What to do about a signal when there is nothing in flight to clean up: leave,
# promptly and successfully. `dump` installs its own disposition for the duration
# of a dump and calls this again afterwards — restoring it rather than clearing
# it, which would have left the loop below deaf to SIGTERM from its second cycle
# onwards.
_idle_traps() {
  trap 'exit 0' HUP INT TERM
}

# Sleep, but wake up for SIGTERM. As PID 1 of its container, `sh` blocked inside
# `sleep` never sees the signal `docker compose down` sends, so this container
# spent the full stop-timeout being killed on every redeploy. Same fix, and the
# same reason, as `docker/periodic.sh` — which this script cannot source, because
# it runs in the Postgres image and is the only file mounted into it.
_nap() {
  sleep "$1" &
  wait $!
}

free_mb() {
  df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'
}

# Remove what a killed run leaves behind: the uncompressed dump it was writing,
# its stderr file, and a half-written archive.
#
# This is not tidiness. The raw dump is the *largest* file this script ever
# creates — bigger than the backup it becomes — and the retention sweep below
# only ever matched `votations-*.sql.gz`, so neither the dot-prefixed raw file
# nor a `.partial` was ever collected. A container killed mid-gzip therefore left
# a full uncompressed dump on the same partition as PostgreSQL, permanently, and
# the next run started that much closer to the full disk this script exists to
# avoid. Two of them and the free-space guard refuses to run at all.
clean_partials() {
  # Older than an hour, so a concurrent run's work in progress is never touched.
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name '.votations-*.sql' -o -name '.votations-*.sql.err' \
       -o -name 'votations-*.sql.gz.partial' \) \
    -mmin +60 -print -delete 2>/dev/null || true
}

# Everything one dump writes. Split out so the caller can clean up after it on
# *every* path: an early `return` does not fire an EXIT trap, so a failing
# pg_dump inside a single function would have left the raw file behind — the very
# thing the trap was added to prevent.
_write_dump() {
  free=$(free_mb "$BACKUP_DIR")
  if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_MB" ]; then
    echo "✗ espace insuffisant : ${free} Mo libres, minimum ${MIN_FREE_MB} Mo." >&2
    echo "  Libérez de l'espace ou copiez les sauvegardes hors de la machine." >&2
    return 1
  fi

  # Dump to a plain file rather than straight into a pipe: the exit status of
  # `pg_dump | gzip` is gzip's, so a missing or failing pg_dump would otherwise
  # look like a successful backup containing nothing.
  if ! pg_dump --no-owner --no-privileges > "$raw" 2>"$raw.err"; then
    echo "✗ pg_dump a échoué : $(head -1 "$raw.err" 2>/dev/null)" >&2
    return 1
  fi

  # A dump that does not contain the votes table is not a backup worth keeping.
  if ! grep -q "CREATE TABLE" "$raw" || ! grep -q "votes" "$raw"; then
    echo "✗ sauvegarde incomplète : ni schéma ni table votes — rejetée" >&2
    return 1
  fi

  gzip -c "$raw" > "$target.partial"
  mv "$target.partial" "$target"
  echo "→ sauvegarde : $target ($(wc -c < "$target") octets)"

  find "$BACKUP_DIR" -name 'votations-*.sql.gz' -type f -mtime "+$KEEP_DAYS" -print -delete

  # Touched only after a dump that passed every check, so the healthcheck reports
  # the age of the last *good* backup rather than the age of the container.
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

  # The trap covers only the abrupt exits — a SIGTERM from `docker compose down`
  # arriving in the middle of a dump, the OOM killer. Every ordinary path, success
  # or failure, is cleaned by the two lines after the call.
  trap 'rm -f "$raw" "$raw.err" "$target.partial"; exit 1' HUP INT TERM

  if _write_dump; then status=0; else status=$?; fi

  # On success these are already gone (moved, or never written); `rm -f` says so
  # without caring which.
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

    # Test the archive *before* asking, and long before dropping anything. The
    # one outcome worse than failing to restore is emptying the database and then
    # discovering the file was truncated. `gunzip -t` reads it through and checks
    # its CRC without writing a byte.
    if ! gunzip -t "$file" 2>/dev/null; then
      echo "✗ archive illisible ou tronquée : $file" >&2
      echo "  Rien n'a été touché. Essayez une sauvegarde plus ancienne." >&2
      exit 1
    fi

    echo "⚠️  Ceci écrase la base ${PGDATABASE:-votations} sur ${PGHOST:-localhost}."
    printf "Taper 'oui' pour confirmer : "
    read -r answer
    [ "$answer" = "oui" ] || { echo "annulé"; exit 1; }

    # Empty the schema first, which is what the line above promises. A dump
    # replayed on top of an existing one stopped at the first « relation already
    # exists » — ON_ERROR_STOP turned that into a half-restored database and a
    # non-zero exit, which is the worst of the three possible outcomes.
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
