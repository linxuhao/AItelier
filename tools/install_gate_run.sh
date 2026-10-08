#!/bin/sh
# Normal operator installation; Source authors must not run this on production.
set -eu
SOURCE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DEST="${1:-$HOME/.AItelier/bin}"
mkdir -p "$DEST"
install -m 755 "$SOURCE/gate_run.sh" "$DEST/gate_run.sh"
install -m 755 "$SOURCE/gate_binding.py" "$DEST/gate_binding.py"
sha256sum "$DEST/gate_run.sh" "$DEST/gate_binding.py"
