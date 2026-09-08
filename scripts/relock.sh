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

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "→ résolution des dépendances déclarées dans pyproject.toml"

# pip-tools in a throwaway virtualenv, never in the project's: it is a build tool
# for this one file and has no business being installed beside the application.
python3 -m venv "$work/venv"
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
