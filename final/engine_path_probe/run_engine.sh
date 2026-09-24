#!/bin/bash
# usage: run_engine.sh <harness.py> <log> <label>
# ONE throwaway container from the sidecar image: --rm, --network none, its own
# /tmp lock paths, the harness under test mounted over /srv/godot_harness.py.
H="$1"; LOG="$2"; LABEL="$3"
D=/home/linxuhao/.AItelier/worktrees-scratch/onereading1-tools/engine
NAME="onereading1-engine-$LABEL"
{
  echo "CMD: docker run --rm --init --name $NAME --network none -u 1000:1000 -m 3g -v $H:/srv/godot_harness.py:ro -v $D:/probe:ro aitelier-godot:latest python3 /probe/drive.py"
  echo "HARNESS: $H  SHA1: $(sha1sum "$H" | cut -d' ' -f1)"
  echo "IMAGE: $(docker image inspect aitelier-godot:latest --format '{{.Id}}')"
  echo "ENGINE_CONTAINERS_RUNNING_AT_LAUNCH: $(docker ps --filter ancestor=aitelier-godot:latest --format '{{.Names}}' | grep -vx aitelier-godot | wc -l)"
  echo "DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$LOG"
docker run --rm --init --name "$NAME" --network none -u 1000:1000 -m 3g \
  -e HOME=/tmp/godot-home \
  -e GODOT_LIFECYCLE_DB=/tmp/ctl/owners.sqlite3 \
  -e GODOT_DEPLOYMENT_LOCK=/tmp/ctl/deployment-admission.lock \
  -e GODOT_RENDER_EFFECT_LOCK=/tmp/ctl/render-effect.lock \
  -e GODOT_BIN=/usr/local/bin/godot \
  -v "$H":/srv/godot_harness.py:ro -v "$D":/probe:ro \
  aitelier-godot:latest python3 /probe/drive.py >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
echo "END: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
exit $RC
