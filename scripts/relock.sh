#!/bin/sh
# Regenerate requirements.lock from the ranges in pyproject.toml.
#
#   scripts/relock.sh
#
# The lockfile is what the image installs, with `--require-hashes`, so nothing
# reaches production that is not pinned *and* fingerprinted. The cost of that is
# that the pins never move on their own: fastapi, starlette, sqlalchemy and the
# rest stay exactly where they were on the day the file was written, security
# releases included. This is the command that moves them, and running it now and
# then is the whole of this project's dependency hygiene.
#
# The same reasoning as `vendor_htmx.sh`, which exists because a vendored file
# rots silently unless refreshing it is one gesture rather than a remembered
# procedure. This one *was* a procedure — four lines in a comment at the top of
# requirements.lock, correct and never run.
#
# Deliberately only this direction. The lockfile header also describes feeding
# the file back to itself to re-hash an unchanged set of versions; that is a rare
# thing to want, and pip-compile annotates the result with the absolute path of
# whatever machine ran it, which is not a thing to commit. Left to the header.
#
# Afterwards, read the diff — it is the list of what moved — then rebuild and run
# the suite against the new set, which is what says whether the move is safe:
#
#   docker compose -f docker-compose.dev.yml build
#   docker compose -f docker-compose.dev.yml run --rm app pytest
set -e

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
target="$root/requirements.lock"

# Resolve under the Python the image runs, not under whatever `python3` happens
# to be here. A resolver picks versions and environment markers against the
# interpreter it is running on, so locking under 3.14 and installing under 3.12
# is how a lockfile comes to describe a set that was never tested — silently,
# because both are valid resolutions of the same ranges.
#
# The version is read off the Dockerfile's `FROM`, which is the thing that
# actually runs it. `requires-python`, the ruff target and the mypy setting all
# name it too; taking a fifth copy here and calling it a constant is how they
# stop agreeing.
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

# pip-tools in a throwaway virtualenv, never in the project's: it is a build tool
# for this one file and has no business being installed beside the application.
#
# This is the step that fails on Debian and Ubuntu, and the failure is confusing
# enough to be worth naming: they ship `ensurepip` in a separate
# `pythonX.Y-venv` package, so the interpreter exists, `-m venv` exists, and
# creating an environment with pip in it fails anyway. One apt away, and there
# is nothing to work around here that is worth the lines it would take.
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

# Run from the project root so the annotations pip-compile writes name
# `pyproject.toml`, not an absolute path that exists on one machine.
cd "$root"
"$work/venv/bin/pip-compile" \
  --quiet --generate-hashes --strip-extras \
  --output-file "$work/requirements.lock" \
  pyproject.toml

# `pip-compile` writes its own invocation into the header. Ours would name a
# python under /tmp, so the file keeps the header it has — the one that explains
# what this file is for and what `--require-hashes` buys.
header_lines=$(awk '/^[^#]/ {print NR - 1; exit}' "$target")
head -n "$header_lines" "$target" > "$work/final"
awk '/^[^#]/ {seen = 1} seen' "$work/requirements.lock" >> "$work/final"
mv "$work/final" "$target"

pinned=$(grep -c '^[a-zA-Z]' "$target")
echo "→ $target : $pinned paquets épinglés et empreintés"
echo "  Relisez le diff, puis reconstruisez l'image et relancez la suite :"
echo "    docker compose -f docker-compose.dev.yml build"
echo "    docker compose -f docker-compose.dev.yml run --rm app pytest"
