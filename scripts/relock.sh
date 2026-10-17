#!/bin/sh
# Regenerate requirements.lock from the ranges in pyproject.toml.
#
#   scripts/relock.sh
#
# The image installs the lockfile with --require-hashes, so dependencies only
# get updated (security fixes included) when this is run. Run it regularly.
#
# Then review the diff, rebuild and run the tests:
#
#   docker compose -f docker-compose.dev.yml build
#   docker compose -f docker-compose.dev.yml run --rm app pytest
set -e

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
target="$root/requirements.lock"

# Resolve with the image's Python version, read from the Dockerfile's FROM.
wanted=$(sed -n 's/^FROM python:\([0-9][0-9.]*\)-.*/\1/p' "$root/Dockerfile" | head -1)
if [ -z "$wanted" ]; then
  echo "✗ impossible de lire la version de Python dans le Dockerfile" >&2
  exit 1
fi

python="python$wanted"
if ! command -v "$python" >/dev/null 2>&1; then
  have=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  echo "✗ $python est introuvable, et python3 est $have." >&2
  echo "  L'image tourne sous $wanted : verrouiller sous $have produirait un jeu" >&2
  echo "  de versions que personne n'exécute. Installez $python, ou lancez ce" >&2
  echo "  script dans le conteneur, qui l'a déjà :" >&2
  echo "    docker compose -f docker-compose.dev.yml run --rm app scripts/relock.sh" >&2
  exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "→ résolution des dépendances déclarées dans pyproject.toml (Python $wanted)"

# pip-tools in a temporary virtualenv. On Debian/Ubuntu this needs the
# pythonX.Y-venv package.
if ! "$python" -m venv "$work/venv" >/dev/null 2>&1; then
  echo "✗ $python ne peut pas créer d'environnement virtuel avec pip." >&2
  echo "  Sur Debian/Ubuntu, ensurepip est dans un paquet à part :" >&2
  echo "    sudo apt install python$wanted-venv" >&2
  echo "  Ou lancez ce script dans le conteneur, qui n'a pas ce découpage :" >&2
  echo "    docker compose -f docker-compose.dev.yml run --rm app scripts/relock.sh" >&2
  exit 1
fi
"$work/venv/bin/pip" install --quiet --upgrade pip
"$work/venv/bin/pip" install --quiet pip-tools

# From the project root, so annotations say `pyproject.toml`, not an absolute path.
cd "$root"
"$work/venv/bin/pip-compile" \
  --quiet --generate-hashes --strip-extras \
  --output-file "$work/requirements.lock" \
  pyproject.toml

# Keep the existing header rather than pip-compile's (which names a /tmp path).
header_lines=$(awk '/^[^#]/ {print NR - 1; exit}' "$target")
head -n "$header_lines" "$target" > "$work/final"
awk '/^[^#]/ {seen = 1} seen' "$work/requirements.lock" >> "$work/final"
mv "$work/final" "$target"

pinned=$(grep -c '^[a-zA-Z]' "$target")
echo "→ $target : $pinned paquets épinglés et empreintés"
echo "  Relisez le diff, puis reconstruisez l'image et relancez la suite :"
echo "    docker compose -f docker-compose.dev.yml build"
echo "    docker compose -f docker-compose.dev.yml run --rm app pytest"
