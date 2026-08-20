#!/bin/sh
# Refresh the vendored copy of htmx, verifying it against npm's own hash.
#
#   scripts/vendor_htmx.sh            # install the version pinned below
#   scripts/vendor_htmx.sh 2.0.11     # install a specific version
#
# htmx is checked in rather than pulled from a CDN: the Content-Security-Policy
# is `script-src 'self'` with no third-party host, and a civic platform should
# not hand its script execution to someone else's uptime. The cost of that choice
# is that the file rots silently unless there is a documented way to refresh it —
# which is what this is.
#
# The tarball is checked against the `dist.integrity` value published by the npm
# registry for that exact version, so a tampered mirror or a truncated download
# fails loudly instead of landing in the repository.
set -e

VERSION="${1:-2.0.10}"
TARGET="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/app/static/htmx.min.js"
REGISTRY="https://registry.npmjs.org/htmx.org"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "→ htmx $VERSION"

# The integrity string npm publishes for this version, e.g. "sha512-<base64>".
integrity=$(
  curl -fsSL "$REGISTRY/$VERSION" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist"]["integrity"])'
)
echo "  intégrité annoncée : $integrity"

curl -fsSL "$REGISTRY/-/htmx.org-$VERSION.tgz" -o "$work/htmx.tgz"

actual=$(
  python3 - "$work/htmx.tgz" <<'PY'
import base64, hashlib, sys
digest = hashlib.sha512(open(sys.argv[1], "rb").read()).digest()
print("sha512-" + base64.b64encode(digest).decode())
PY
)

if [ "$actual" != "$integrity" ]; then
  echo "✗ empreinte incohérente" >&2
  echo "  attendu : $integrity" >&2
  echo "  obtenu  : $actual" >&2
  exit 1
fi
echo "  empreinte vérifiée"

tar -xzf "$work/htmx.tgz" -C "$work" package/dist/htmx.min.js
cp "$work/package/dist/htmx.min.js" "$TARGET"

echo "→ écrit dans $TARGET ($(wc -c < "$TARGET") octets)"
echo "  Relancez la suite : les pages qui utilisent htmx sont la page de loi et"
echo "  le formulaire d'administration (bandeau de brouillon)."
