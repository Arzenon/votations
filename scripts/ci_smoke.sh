#!/bin/sh
# Start the production stack with the image `votations-app:local` and check
# that it answers as it should, through Caddy. Run by the CI before publishing
# the image; also runs locally:
#
#   docker build --target base -t votations-app:local .
#   scripts/ci_smoke.sh
#
# Works in a temporary directory with a throwaway .env, under its own compose
# project, so it never reads your .env nor touches another stack's volumes.
# Caddy is published on local ports of its own (SMOKE_HTTP_PORT and
# SMOKE_HTTPS_PORT) rather than 80 and 443, which are often taken.
# The import jobs are not started: they would hit public servers on every run.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT=votations-smoke
SERVICES="db app caddy db-backup content-export"
TIMEOUT="${SMOKE_TIMEOUT:-180}"
HTTP_PORT="${SMOKE_HTTP_PORT:-18080}"
HTTPS_PORT="${SMOKE_HTTPS_PORT:-18443}"
SITE="https://localhost:$HTTPS_PORT"

work="$(mktemp -d)"

compose() {
  docker compose -p "$PROJECT" --project-directory "$work" \
    -f "$work/docker-compose.prod.yml" -f "$work/ports.yml" "$@"
}

fail() {
  echo "✗ $*" >&2
  exit 1
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # Throwaway data: the logs can be shown in full.
    compose ps -a || true
    compose logs --no-color --tail=80 || true
  fi
  compose down -v --remove-orphans >/dev/null || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

random() {
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# The files the stack reads from its directory.
cp "$ROOT/docker-compose.prod.yml" "$ROOT/Caddyfile" "$work/"
mkdir -p "$work/scripts" "$work/exports"
cp "$ROOT/scripts/backup.sh" "$work/scripts/"

# Replaces the stack's 80 and 443 (`!override` needs Compose 2.24 or later).
cat > "$work/ports.yml" <<EOF
services:
  caddy:
    ports: !override
      - "127.0.0.1:$HTTP_PORT:80"
      - "127.0.0.1:$HTTPS_PORT:443"
EOF

cat > "$work/.env" <<EOF
SITE_DOMAIN=localhost
POSTGRES_PASSWORD=$(random)
SECRET_KEY=$(random)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$(random)
MIN_BUCKET_VOLUME=50
MIN_CELL_COUNT=10
EXPORT_TIME_RESOLUTION=day
LEGAL_CONTACT_EMAIL=
LEGAL_HOST=
ANTHROPIC_API_KEY=
EXPORT_UID=$(id -u)
EXPORT_GID=$(id -g)
EOF

# Leftovers of an interrupted run: Postgres would keep that run's password and
# refuse this one's.
compose down -v --remove-orphans >/dev/null 2>&1 || true

echo "→ démarrage de la pile de production ($SERVICES)"
# shellcheck disable=SC2086
compose up -d --no-build $SERVICES

echo "→ attente de l'application"
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null || true)
  [ "$status" = healthy ] && break
  [ "$status" = unhealthy ] && fail "l'application est en mauvaise santé"
  [ "$(date +%s)" -lt "$deadline" ] || fail "l'application n'est pas prête après ${TIMEOUT}s"
  sleep 3
done

# Caddy issues its own certificate for localhost, hence -k.
http_code() {
  curl -ks -o /dev/null -w '%{http_code}' "$@"
}

expect() {
  expected="$1"
  shift
  got=$(http_code "$@")
  [ "$got" = "$expected" ] || fail "$*: HTTP $got au lieu de $expected"
  echo "  ok  $expected  $*"
}

echo "→ vérifications à travers Caddy"
# The first TLS request can arrive before Caddy has its certificate.
i=0
until [ "$(http_code "$SITE/")" = 200 ]; do
  i=$((i + 1))
  [ "$i" -lt 10 ] || break
  sleep 2
done

expect 200 "$SITE/"
expect 200 "$SITE/recherche"
expect 200 "$SITE/mentions-legales"
expect 200 "$SITE/vie-privee"
expect 404 "$SITE/sante"
expect 308 "http://localhost:$HTTP_PORT/"
expect 301 "https://www.localhost:$HTTPS_PORT/"

headers=$(curl -ksSI "$SITE/")
for header in content-security-policy strict-transport-security x-frame-options; do
  printf '%s\n' "$headers" | grep -qi "^$header:" || fail "en-tête absent : $header"
done
printf '%s\n' "$headers" | grep -qi '^server:' && fail "Caddy annonce son en-tête Server"
echo "  ok  en-têtes de sécurité"

# The admin is behind HTTP Basic.
expect 401 "$SITE/admin"

echo "→ sauvegarde de la base"
compose exec -T db-backup sh /usr/local/bin/backup.sh dump >/dev/null \
  || fail "la sauvegarde a échoué"

echo "→ état des services"
for service in $SERVICES; do
  id=$(compose ps -q "$service")
  state=$(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$id")
  [ "$state" = "running 0" ] || fail "$service : $state (attendu : running, sans redémarrage)"
done

echo "✓ la pile de production répond"
