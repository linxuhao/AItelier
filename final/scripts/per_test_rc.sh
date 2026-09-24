#!/bin/bash
# usage: per_test_rc.sh <tree> <log> <name> <test file>
# In ONE throwaway container: collect every test id in <test file>, then run
# each id as its own pytest process and write its bare exit code, then run the
# whole file once with -rA --tb=line so every failure reason is in the log.
TREE="$1"; LOG="$2"; NAME="$3"; FILE="$4"
T=/home/linxuhao/.AItelier/worktrees-scratch/onereading2-tools
throwaway() { docker ps --format '{{.Names}}' | grep -vxF -f "$T/long_running.txt"; }
while [ "$(throwaway | wc -l)" -ge 4 ]; do sleep 5; done
{
  echo "TREE: $(git -C "$TREE" rev-parse HEAD)  DIRTY_FILES: $(git -C "$TREE" status --porcelain | tr '\n' ' ')"
  echo "FILE: $FILE  SHA1: $(sha1sum "$TREE/$FILE" | cut -d' ' -f1)  HARNESS_SHA1: $(sha1sum "$TREE/docker/godot/godot_harness.py" | cut -d' ' -f1)"
  echo "CONTAINER: $NAME  DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)  THROWAWAY_AT_LAUNCH: $(throwaway | wc -l)"
} > "$LOG"
docker run --rm --init --name "$NAME" -m 3g --network aitelier_default -u 1000:1000 \
  -e HOME=/home/linxuhao -e PYTHONPATH="$TREE" -e PYTHONDONTWRITEBYTECODE=1 \
  -e SEARXNG_URL=http://linxuhaserver:8888 \
  -v /home/linxuhao/AItelier:/app -v /home/linxuhao/AItelier:/home/linxuhao/AItelier \
  -v /home/linxuhao/.AItelier:/home/linxuhao/.AItelier -w "$TREE" aitelier:latest \
  sh -c 'F="$1"
echo "IN_CONTAINER_GIT_STATUS_RC: $(git status --porcelain >/dev/null 2>&1; echo $?)"
python -m pytest -p no:cacheprovider -q --collect-only "$F" 2>/dev/null | grep "::" > /tmp/ids.txt
echo "IDS: $(wc -l < /tmp/ids.txt)"
while read -r id; do
  python -m pytest -p no:cacheprovider -q "$id" > /dev/null 2>&1
  echo "PER_TEST $id BARE_RC=$?"
done < /tmp/ids.txt
echo "=== whole file, -rA --tb=line"
python -m pytest -p no:cacheprovider -q -rA --tb=line "$F"
echo "WHOLE_FILE_BARE_RC=$?"' sh "$FILE" >> "$LOG" 2>&1
RC=$?
echo "CONTAINER_BARE_RC=$RC" >> "$LOG"
echo "END: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
exit $RC
