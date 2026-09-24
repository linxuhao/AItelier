#!/bin/bash
# usage: runsuite.sh <tree> <log> <name> [pytest args...]
# Runs pytest in a throwaway aitelier:latest container with --init; writes the
# bare exit code into the log as BARE_RC=. No pipe on the pytest command.
TREE="$1"; LOG="$2"; NAME="$3"; shift 3
mkdir -p "$(dirname "$LOG")"
SLOTS=$(docker ps --filter ancestor=aitelier:latest --format '{{.Names}}' | grep -vx aitelier | wc -l)
{
  echo "CMD: pytest $*"
  echo "TREE: $(git -C "$TREE" rev-parse HEAD)  DIRTY_FILES: $(git -C "$TREE" status --porcelain | wc -l)"
  echo "CWD: $TREE  CONTAINER: $NAME  DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "SLOTS_IN_USE_AT_LAUNCH: $SLOTS"
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
