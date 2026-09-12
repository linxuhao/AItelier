#!/bin/sh
# zvec-grep sidecar entrypoint: the search-only MCP daemon for agents, plus a
# host-owned indexer loop. Repositories under both projects/ and run-owned
# worktrees/ become searchable without granting index/drop tools to agents.
set -u
PROJECTS="${AITELIER_PROJECTS_DIR:-/home/linxuhao/.AItelier/projects}"
WORKTREES="${AITELIER_WORKTREES_DIR:-/home/linxuhao/.AItelier/worktrees}"
ZVEC_HOME="${ZVEC_GREP_HOME:-/home/linxuhao/.AItelier/zvec-grep-home}"
EMBED="${ZVEC_GREP_EMBEDDING:-local/potion-code-16m-v2}"

repo_git_exclude() {
  git -C "$1" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null
}

prepare_repo() {
  repo="$1"
  [ -e "$repo/.git" ] || return 1
  git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  ex="$(repo_git_exclude "$repo")" || {
    echo "[zg-indexer] cannot resolve git exclude; skipping: $repo" >&2
    return 1
  }
  mkdir -p "$(dirname "$ex")" || {
    echo "[zg-indexer] cannot create git metadata; skipping: $repo" >&2
    return 1
  }
  if ! grep -Fqx '.zvec-grep/' "$ex" 2>/dev/null; then
    printf '%s\n' '.zvec-grep/' >> "$ex" || {
      echo "[zg-indexer] cannot exclude .zvec-grep; skipping: $repo" >&2
      return 1
    }
  fi
}

index_repo() {
  repo="$1"
  prepare_repo "$repo" || return 1
  [ -d "$repo/.zvec-grep" ] && return 1
  echo "[zg-indexer] indexing new repo: $repo"
  if index_output="$(zg index "$repo" --embedding "$EMBED" --mode server \
      --hidden --glob '!**/.zvec-grep/**' 2>&1)"; then
    printf '%s\n' "$index_output" | tail -2 | sed 's/^/[zg-indexer] /'
  else
    printf '%s\n' "$index_output" | tail -2 | sed 's/^/[zg-indexer] /' >&2
    echo "[zg-indexer] index failed; will retry next pass: $repo" >&2
  fi
}

next_repo() {
  parent="$1"
  order="$2"
  attempted="$3"
  [ -d "$parent" ] || return 1
  best=""
  best_mtime=-1
  for candidate in "$parent"/*/; do
    candidate="${candidate%/}"
    [ -L "$candidate" ] && continue
    [ -e "$candidate/.git" ] || continue
    [ -d "$candidate/.zvec-grep" ] && continue
    grep -Fqx "$candidate" "$attempted" 2>/dev/null && continue
    if [ "$order" = "first" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    mtime="$(stat -c %Y "$candidate" 2>/dev/null || printf 0)"
    if [ "$mtime" -gt "$best_mtime" ]; then
      best="$candidate"
      best_mtime="$mtime"
    fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

index_new_repos() {
  # One writer stays strictly serial. Each batch gives four slots to the
  # newest run worktrees and one to the projects backlog, then rescans so a
  # just-created run never waits behind the startup inventory.
  attempted="$(mktemp "${TMPDIR:-/tmp}/aitelier-zg-attempted.XXXXXX")" || {
    echo "[zg-indexer] cannot create scheduling state; skipping pass" >&2
    return 1
  }
  while :; do
    scheduled=0
    worktree_slots=0
    while [ "$worktree_slots" -lt 4 ]; do
      repo="$(next_repo "$WORKTREES" newest "$attempted")" || break
      printf '%s\n' "$repo" >> "$attempted"
      index_repo "$repo" || true
      worktree_slots=$((worktree_slots + 1))
      scheduled=1
    done
    if repo="$(next_repo "$PROJECTS" first "$attempted")"; then
      printf '%s\n' "$repo" >> "$attempted"
      index_repo "$repo" || true
      scheduled=1
    fi
    [ "$scheduled" -eq 1 ] || break
  done
  rm -f "$attempted"
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
trap 'kill -TERM "$SERVER" "$PROXY" 2>/dev/null; wait "$SERVER"; exit 0' TERM INT

sleep 5
while kill -0 "$SERVER" 2>/dev/null; do
  index_new_repos
  sleep 60 &
  wait $!
done
wait "$SERVER"
