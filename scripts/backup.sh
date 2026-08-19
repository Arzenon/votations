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

dump() {
  mkdir -p "$BACKUP_DIR"
  stamp=$(date -u +%Y%m%d-%H%M%S)
  target="$BACKUP_DIR/votations-$stamp.sql.gz"
  raw="$BACKUP_DIR/.votations-$stamp.sql"

  # Dump to a plain file rather than straight into a pipe: the exit status of
  # `pg_dump | gzip` is gzip's, so a missing or failing pg_dump would otherwise
  # look like a successful backup containing nothing.
  if ! pg_dump --no-owner --no-privileges > "$raw" 2>"$raw.err"; then
    echo "✗ pg_dump a échoué : $(head -1 "$raw.err" 2>/dev/null)" >&2
    rm -f "$raw" "$raw.err"
    return 1
  fi
  rm -f "$raw.err"

  # A dump that does not contain the votes table is not a backup worth keeping.
  if ! grep -q "CREATE TABLE" "$raw" || ! grep -q "votes" "$raw"; then
    echo "✗ sauvegarde incomplète : ni schéma ni table votes — rejetée" >&2
    rm -f "$raw"
    return 1
  fi

  gzip -c "$raw" > "$target.partial"
  rm -f "$raw"
  mv "$target.partial" "$target"
  echo "→ sauvegarde : $target ($(wc -c < "$target") octets)"

  find "$BACKUP_DIR" -name 'votations-*.sql.gz' -type f -mtime "+$KEEP_DAYS" -print -delete
}

case "${1:-dump}" in
  dump)
    dump
    ;;
  loop)
    echo "→ sauvegarde toutes les ${INTERVAL}s, conservation ${KEEP_DAYS} jours"
    while true; do
      dump || echo "→ nouvel essai au prochain cycle"
      sleep "$INTERVAL"
    done
    ;;
  list)
    ls -lh "$BACKUP_DIR"/votations-*.sql.gz 2>/dev/null || echo "aucune sauvegarde"
    ;;
  restore)
    file="$2"
    [ -n "$file" ] || { echo "usage: backup.sh restore <fichier.sql.gz>" >&2; exit 2; }
    [ -f "$file" ] || { echo "introuvable : $file" >&2; exit 2; }
    echo "⚠️  Ceci écrase la base ${PGDATABASE:-votations} sur ${PGHOST:-localhost}."
    printf "Taper 'oui' pour confirmer : "
    read -r answer
    [ "$answer" = "oui" ] || { echo "annulé"; exit 1; }
    gunzip -c "$file" | psql --quiet --set ON_ERROR_STOP=1
    echo "→ restauration terminée depuis $file"
    ;;
  *)
    echo "usage: backup.sh [dump|loop|list|restore <fichier>]" >&2
    exit 2
    ;;
esac
