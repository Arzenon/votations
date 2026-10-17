#!/bin/sh
# Deploy a commit of master: the image the CI tested for it, and the compose
# files of the same commit. Rolls back if the app doesn't come up healthy.
#
#   scripts/deploy.sh <commit>
#
# Over SSH, it's the forced command of the CI's key and reads the commit from
# $SSH_ORIGINAL_COMMAND (see DEPLOY.md).
#
# Output ends up in the public CI logs: never print the app's logs here.
set -eu

IMAGE_REPO="${VOTATIONS_IMAGE_REPO:-ghcr.io/arzenon/votations}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-180}"

# The tag every service of docker-compose.prod.yml runs.
CURRENT=votations-app:local
PREVIOUS=votations-app:previous

log() { printf '→ %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f docker-compose.prod.yml "$@"; }

wait_healthy() {
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null || true)
    case "$status" in
      healthy) return 0 ;;
      unhealthy) return 1 ;;
    esac
    sleep 5
  done
  return 1
}

# Caddyfile and backup.sh are bind-mounted single files: a checkout replaces
# them on disk, and the running container keeps the old one until restarted.
restart_changed() {
  if ! git diff --quiet "$1" "$2" -- Caddyfile; then
    log "Caddyfile modifié : redémarrage de caddy"
    compose restart caddy || return 1
  fi
  if ! git diff --quiet "$1" "$2" -- scripts/backup.sh; then
    compose restart db-backup || return 1
  fi
}

caddy_running() {
  # Gives a Caddy that rejects its config the time to crash.
  sleep 5
  [ "$(docker inspect --format '{{.State.Status}}' "$(compose ps -q caddy)" 2>/dev/null)" = running ]
}

# Chained with &&: callers handle the failure, and `set -e` doesn't apply
# inside a function called from a condition.
switch_to() {
  git checkout --quiet --detach "$1" \
    && docker tag "$2" "$CURRENT" \
    && compose up -d --no-build \
    && restart_changed "$3" "$1"
}

rollback() {
  if [ "$can_roll_back" != 1 ]; then
    die "aucune version précédente à rétablir : intervention manuelle"
  fi
  log "retour à la version précédente (${previous_commit})"
  switch_to "$previous_commit" "$PREVIOUS" "$commit" \
    || die "impossible de rétablir la version précédente : intervention manuelle"
  if wait_healthy && caddy_running; then
    log "version précédente rétablie"
  else
    log "la version précédente ne répond pas non plus : intervention manuelle"
  fi
  cat >&2 <<EOF
✗ le déploiement de $commit a échoué.
  Si ce commit apportait une migration, elle a pu être appliquée : l'ancien
  code tourne alors sur le nouveau schéma. La base a été sauvegardée juste
  avant (backup.sh list). Diagnostic, sur le serveur :
    docker compose -f docker-compose.prod.yml logs --tail=100 app
EOF
  exit 1
}

main() {
  commit="${1:-${SSH_ORIGINAL_COMMAND:-}}"
  case "$commit" in
    "" | *[!0-9a-f]*) die "usage : deploy.sh <commit> (identifiant complet, 40 caractères)" ;;
  esac
  [ "${#commit}" -eq 40 ] || die "identifiant de commit incomplet : $commit"

  cd "$(dirname "$0")/.."

  exec 9>.deploy.lock
  flock -n 9 || die "un déploiement est déjà en cours"

  log "vérification de $commit"
  git fetch --quiet origin master
  git merge-base --is-ancestor "$commit" origin/master 2>/dev/null \
    || die "$commit n'est pas sur master : refusé"
  [ -z "$(git status --porcelain --untracked-files=no)" ] \
    || die "le dépôt du serveur a des modifications locales : rien n'a été changé"

  image="$IMAGE_REPO:sha-$commit"
  log "téléchargement de $image"
  docker pull --quiet "$image" >/dev/null \
    || die "image introuvable : $image (publiée par la CI, et le paquet est-il public ?)"

  # What to switch back to if the new version doesn't come up.
  previous_commit=$(git rev-parse HEAD)
  stale_previous=$(docker image inspect --format '{{.Id}}' "$PREVIOUS" 2>/dev/null || true)
  can_roll_back=0
  if docker image inspect "$CURRENT" >/dev/null 2>&1; then
    docker tag "$CURRENT" "$PREVIOUS"
    can_roll_back=1
  fi

  # Migrations run when the app starts: the dump is the only way back for data.
  log "sauvegarde de la base"
  compose run --rm -T --entrypoint sh db-backup /usr/local/bin/backup.sh dump \
    || die "sauvegarde impossible : rien n'a été changé"

  log "passage à $commit"
  switch_to "$commit" "$image" "$previous_commit" \
    || { log "le changement de version a échoué"; rollback; }

  log "attente de l'application (${HEALTH_TIMEOUT}s au plus)"
  wait_healthy || { log "l'application ne répond pas"; rollback; }
  caddy_running || { log "caddy ne démarre pas"; rollback; }

  # Keep the deployed image and the previous one, drop the rest.
  docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE_REPO" \
    | grep -vxF "$image" | xargs -r docker rmi >/dev/null 2>&1 || true
  if [ -n "$stale_previous" ]; then
    docker rmi "$stale_previous" >/dev/null 2>&1 || true
  fi

  log "déployé : $commit"
}

# On one line: the checkout replaces this file while it runs, and sh reads a
# script as it goes. Everything else is inside functions, already parsed.
main "$@"; exit $?
