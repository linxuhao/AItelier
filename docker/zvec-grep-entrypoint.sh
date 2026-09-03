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

zg server run --listen 127.0.0.1:7999 --mcp-toolset agent &
SERVER=$!

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
  sleep 60
done
wait "$SERVER"
