#!/bin/bash
# usage: run.sh <name> <tree> <logfile> -- <command...>
# Runs <command> in a throwaway aitelier:latest container, mounted as the
# production container is (the checkout at /app and /home/linxuhao/AItelier,
# ~/.AItelier at its own path, so the worktree's gitdir resolves), with <tree>
# as the working directory and first on PYTHONPATH. Refuses to start when 4
# throwaway containers already run on the server.
set -u
NAME="$1"; TREE="$2"; LOG="$3"; shift 3; [ "$1" = "--" ] && shift
TOTAL=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^aitelier(:latest)?$/ && $1 != "aitelier"' | wc -l)
{
echo "date: $(date -u +%FT%TZ)"
echo "tree: $TREE host head: $(git -C "$TREE" rev-parse HEAD)"
echo "host worktree status (porcelain):"; git -C "$TREE" status --porcelain
echo "throwaway containers (aitelier image, not prod) before launch: $TOTAL"
echo "uid: ${RUN_UID:-1000}"
echo "command: $*"
} > "$LOG"
if [ "$TOTAL" -ge 4 ]; then echo "REFUSED: 4 throwaway containers already running" >> "$LOG"; exit 99; fi
docker run --rm --init -m 3g --name "$NAME" --network aitelier_default \
  -u "${RUN_UID:-1000}:${RUN_UID:-1000}" \
  -e HOME=/home/linxuhao -e PYTHONPATH="$TREE" -e PYTHONDONTWRITEBYTECODE=1 \
  ${RUN_ENV:+-e "$RUN_ENV"} \
  -v /home/linxuhao/AItelier:/app -v /home/linxuhao/AItelier:/home/linxuhao/AItelier \
  -v /home/linxuhao/.AItelier:/home/linxuhao/.AItelier \
  -w "$TREE" aitelier:latest \
  bash -c 'echo "in-container git head: $(git -c safe.directory="*" -C . rev-parse HEAD) status-lines: $(git -c safe.directory="*" -C . status --porcelain | wc -l)"; python -c "import aitelier.tools.run_tests.impl as a, core.gate_deferral as g, skillflow; print(\"IMPORT\", a.__file__); print(\"IMPORT\", g.__file__); print(\"IMPORT skillflow\", skillflow.__file__, getattr(skillflow,\"__version__\",\"?\"))"; exec "$@"' _ "$@" >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
echo "end: $(date -u +%FT%TZ)" >> "$LOG"
exit $RC
