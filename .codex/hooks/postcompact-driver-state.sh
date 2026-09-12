#!/bin/sh
set -eu
HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 -B "$HOOK_DIR/postcompact_driver_state.py"
