#!/bin/sh
# zvec-grep sidecar entrypoint: the search-only MCP daemon for agents, plus a
# host-owned control worker. Projects retain automatic baseline indexing;
# run worktrees are prepared only from recorded owner demand.
set -u
PROJECTS="${AITELIER_PROJECTS_DIR:-/home/linxuhao/.AItelier/projects}"
WORKTREES="${AITELIER_WORKTREES_DIR:-/home/linxuhao/.AItelier/worktrees}"
ZVEC_HOME="${ZVEC_GREP_HOME:-/home/linxuhao/.AItelier/zvec-grep-home}"
EMBED="${ZVEC_GREP_EMBEDDING:-local/potion-code-16m-v2}"

CONTROL="${AITELIER_HOME:-/home/linxuhao/.AItelier}/semantic-index-control"
mkdir -p "$CONTROL" || exit 1
# Lock before clearing daemon metadata or starting any process. A competing
# sidecar cannot run either the old scanner or a second control writer.
exec 9>"$CONTROL/service.lock"
flock -n 9 || { echo "[zg-lifecycle] sidecar writer already owns service" >&2; exit 75; }
rm -f "$ZVEC_HOME/daemon/instance.lock"
for parent in "$PROJECTS" "$WORKTREES"; do
  [ -d "$parent" ] || continue
  rm -f "$parent"/*/.zvec-grep/locks/daemon.json
done
zg server run --listen 127.0.0.1:7999 --mcp-toolset agent &
SERVER=$!
node /usr/local/lib/zvec-grep-proxy.js &
PROXY=$!
WORKER=""
cleanup() {
  [ -z "$WORKER" ] || kill -TERM "$WORKER" 2>/dev/null
  kill -TERM "$PROXY" "$SERVER" 2>/dev/null
  [ -z "$WORKER" ] || wait "$WORKER" 2>/dev/null
  wait "$SERVER" 2>/dev/null
}
trap 'cleanup; exit 0' TERM INT
tries=0
until zg server status --check-ready >/dev/null 2>&1; do
  if ! kill -0 "$SERVER" 2>/dev/null || [ "$tries" -ge 30 ]; then
    cleanup; exit 1
  fi
  sleep 1
  tries=$((tries + 1))
done
python3 /usr/local/lib/semantic_index_control.py --control-dir "$CONTROL" \
  --worktrees-root "$WORKTREES" --projects-root "$PROJECTS" &
WORKER=$!
while kill -0 "$SERVER" 2>/dev/null && kill -0 "$WORKER" 2>/dev/null; do
  sleep 1 &
  wait $!
done
cleanup
exit 1
