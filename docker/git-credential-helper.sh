#!/bin/sh
# Git credential helper for AItelier's container.
#
# Feeds the GITHUB_TOKEN Docker secret to GitHub HTTPS remotes ONLY. The token
# lives in the secret file (/run/secrets/GITHUB_TOKEN), never in the
# environment or the workspace tree — same model as the LLM API key. This is
# strictly smaller blast radius than bind-mounting ~/.git-credentials (which
# would expose the host's entire credential store to in-container code).
#
# Wired in via GIT_CONFIG_* in docker-compose.yml so every git invocation
# (clone / fetch / push) picks it up with no per-command config.
#
# Git calls the helper as:  helper <operation>   with request attrs on stdin.
# We only answer "get" for github.com; everything else exits silently so git
# falls through to its normal (anonymous) behaviour.

[ "$1" = "get" ] || exit 0

SECRET="${AITELIER_GITHUB_TOKEN_FILE:-/run/secrets/GITHUB_TOKEN}"
# -s: exists AND non-empty, so an empty placeholder token file behaves as "no
# credentials" (public clones still work; private auth simply isn't offered).
[ -s "$SECRET" ] || exit 0

host=""
while IFS= read -r line; do
  case "$line" in
    host=*) host="${line#host=}" ;;
    "") break ;;
  esac
done

case "$host" in
  github.com|*.github.com) ;;
  *) exit 0 ;;
esac

echo "username=x-access-token"
echo "password=$(cat "$SECRET")"
