#!/bin/sh
# zvec-grep sidecar entrypoint: the search-only MCP daemon for agents, plus a
# host-owned lifecycle worker. Only recorded run demand creates indexes;
# retained checkout directories are not an indexing queue.
set -u
DATA="${AITELIER_HOME:-/home/linxuhao/.AItelier}"
PROJECTS="${AITELIER_PROJECTS_DIR:-$DATA/projects}"
WORKTREES="${AITELIER_WORKTREES_DIR:-$DATA/worktrees}"
ZVEC_HOME="${ZVEC_GREP_HOME:-$DATA/zvec-grep-home}"
WORKER_SCRIPT="${AITELIER_ZG_WORKER_SCRIPT:-/usr/local/lib/semantic_index_control.py}"

process_index_requests() {
  # The background PID is the Python worker itself, so TERM reaches its handler.
  exec python3 "$WORKER_SCRIPT" --once \
    --control-dir "$(dirname "$WORKTREES")/semantic-index-control" \
    --worktrees-root "$WORKTREES"
}

# Unit tests source the functions without starting processes or touching the
# production daemon. This is intentionally not a runtime/agent-facing switch.
if [ "${AITELIER_ZG_INDEXER_LIB_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

# A container restart can leave daemon and per-index leases behind. This
# sidecar is the sole index writer, so only its known roots are cleared.
rm -f "$ZVEC_HOME/daemon/instance.lock"
for parent in "$PROJECTS" "$WORKTREES"; do
  [ -d "$parent" ] || continue
  rm -f "$parent"/*/.zvec-grep/locks/daemon.json
done

# zg refuses non-loopback binds. The proxy exposes it only on the compose-private
# network. TERM is forwarded so daemon-owned locks are normally removed.
zg server run --listen 127.0.0.1:7999 --mcp-toolset agent &
SERVER=$!
node /usr/local/lib/zvec-grep-proxy.js &
PROXY=$!
INDEXER=""
SLEEP_PID=""
stop_children() {
  trap - TERM INT EXIT
  for pid in "$INDEXER" "$SLEEP_PID" "$PROXY" "$SERVER"; do
    [ -z "$pid" ] || kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "$INDEXER" "$SLEEP_PID" "$PROXY" "$SERVER"; do
    [ -z "$pid" ] || wait "$pid" 2>/dev/null || true
  done
}
trap 'stop_children; exit 0' TERM INT
trap stop_children EXIT

while kill -0 "$SERVER" 2>/dev/null && kill -0 "$PROXY" 2>/dev/null; do
  process_index_requests &
  INDEXER=$!
  wait "$INDEXER" || exit 1
  INDEXER=""
  sleep 1 &
  SLEEP_PID=$!
  wait "$SLEEP_PID"
  SLEEP_PID=""
done
exit 1
