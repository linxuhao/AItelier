#!/bin/bash
# usage: runsuite.sh <tree> <log> <name> [pytest args...]
# Runs pytest in a throwaway aitelier:latest container with --init; writes the
# bare exit code into the log as BARE_RC=. No pipe on the pytest command.
# Launch waits until fewer than 4 throwaway containers run server-wide
# (every running container not named in long_running.txt counts).
TREE="$1"; LOG="$2"; NAME="$3"; shift 3
T=/home/linxuhao/.AItelier/worktrees-scratch/onereading2-tools
mkdir -p "$(dirname "$LOG")"
throwaway() { docker ps --format '{{.Names}}' | grep -vxF -f "$T/long_running.txt"; }
while [ "$(throwaway | wc -l)" -ge 4 ]; do sleep 5; done
{
  echo "CMD: pytest $*"
  echo "TREE: $(git -C "$TREE" rev-parse HEAD)  DIRTY_FILES: $(git -C "$TREE" status --porcelain | wc -l)"
  echo "CWD: $TREE  CONTAINER: $NAME  DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "THROWAWAY_CONTAINERS_AT_LAUNCH(server-wide, before this one): $(throwaway | wc -l) [$(throwaway | tr '\n' ' ')]"
} > "$LOG"
docker run --rm --init --name "$NAME" -m 3g --network aitelier_default -u 1000:1000 \
  -e HOME=/home/linxuhao -e PYTHONPATH="$TREE" -e PYTHONDONTWRITEBYTECODE=1 \
  -e SEARXNG_URL=http://linxuhaserver:8888 \
  -v /home/linxuhao/AItelier:/app -v /home/linxuhao/AItelier:/home/linxuhao/AItelier \
  -v /home/linxuhao/.AItelier:/home/linxuhao/.AItelier -w "$TREE" aitelier:latest \
  sh -c 'echo "IN_CONTAINER_GIT_STATUS_RC: $(git status --porcelain >/dev/null 2>&1; echo $?)"; exec python -m pytest -p no:cacheprovider -q -rf "$@"' sh "$@" >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
echo "END: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
exit $RC
