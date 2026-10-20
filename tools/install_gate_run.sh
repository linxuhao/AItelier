#!/bin/sh
# Normal operator installation; Source authors must not run this on production.
set -eu
SOURCE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$SOURCE/install_gate_run.py" "$SOURCE" "$@"
