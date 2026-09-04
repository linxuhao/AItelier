#!/bin/sh
# zvec-grep sidecar entrypoint: the search-only MCP daemon for the agents, plus
# a host-owned indexer loop so a NEW project is searchable within a minute
# without any agent being able to build, rebuild or drop an index.
#   - every project repo under ~/.AItelier/projects/* that has .git and no
#     .zvec-grep/ gets `.zvec-grep/` in .git/info/exclude (repo_apply is
#     `git add -A`) and a first index in the daemon (server mode, so the
#     daemon watches it for refresh afterwards);
#   - existing indexes are refreshed by the daemon's watcher + hourly probe.
set -u
PROJECTS="${AITELIER_PROJECTS_DIR:-/home/linxuhao/.AItelier/projects}"
EMBED="${ZVEC_GREP_EMBEDDING:-local/potion-code-16m-v2}"

# A container restart (SIGKILL on `docker compose restart`) leaves the
# daemon's instance.lock behind and the next boot refuses to start
# ("already running with PID 8"). Nothing else can hold it in this
# container, so clear it.
rm -f /home/linxuhao/.AItelier/zvec-grep-home/daemon/instance.lock
# Same story one level down: each project index carries locks/daemon.json
# naming the daemon that owns its writes by (pid, hostname, token). A
# recreated sidecar gets the SAME hostname and pid 9 again, so the new daemon
# reads the dead one's lease as "another daemon" and every search fails with
# INDEX_BUSY. Only this sidecar ever writes project indexes, so any lease
# present at boot is a corpse.
rm -f "$PROJECTS"/*/.zvec-grep/locks/daemon.json

# zg refuses any non-loopback listen address ([LOOPBACK_REQUIRED]), so the
# daemon stays on 127.0.0.1:7999 and zvec-grep-proxy.js fronts it on
# 0.0.0.0:7998 for the compose-private network (aitelier -> zvec-grep:7998).
# Forward TERM to both so zg exits cleanly (unlinks its instance.lock and
# index leases) instead of being KILLed as an orphan of PID 1.
zg server run --listen 127.0.0.1:7999 --mcp-toolset agent &
SERVER=$!
node /usr/local/lib/zvec-grep-proxy.js &
PROXY=$!
trap 'kill -TERM "$SERVER" "$PROXY" 2>/dev/null; wait "$SERVER"; exit 0' TERM INT

index_new_repos() {
  for repo in "$PROJECTS"/*/; do
    repo="${repo%/}"
    [ -d "$repo/.git" ] || continue
    [ -d "$repo/.zvec-grep" ] && continue
    ex="$repo/.git/info/exclude"
    mkdir -p "$repo/.git/info"
    grep -q '^\.zvec-grep/$' "$ex" 2>/dev/null || echo '.zvec-grep/' >> "$ex"
    echo "[zg-indexer] indexing new repo: $repo"
    zg index "$repo" --embedding "$EMBED" --mode server 2>&1 | tail -2 | sed 's/^/[zg-indexer] /'
  done
}

# wait for the daemon, then loop
sleep 5
while kill -0 "$SERVER" 2>/dev/null; do
  index_new_repos
  sleep 60 &
  wait $!   # a plain `sleep` would hold the trap off for up to a minute
done
wait "$SERVER"
