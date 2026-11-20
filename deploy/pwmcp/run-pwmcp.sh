#!/usr/bin/env bash
# One Playwright MCP browser per driver: own container, port, profile (saves/IndexedDB), output dir.
# Usage: run-pwmcp.sh <driver> <port>      e.g. run-pwmcp.sh grok 8932 ; run-pwmcp.sh claude 8933
#   dsh keeps legacy layout (container dsh-pwmcp, port 8931, $B/profile, $B/out) via run-dsh-pwmcp.sh.
# Pinned: image mcr.microsoft.com/playwright:v1.65.0-noble (chromium-1254), @playwright/mcp@0.0.84
#   (shared install in $PKG = $B/pkg-0.0.84, from deploy/pwmcp/package-lock.json in the AItelier repo).
#   0.0.84 is the first @playwright/mcp whose playwright-core bundles @modelcontextprotocol/sdk 1.32.0
#   (GHSA-f897-62hf-6gxq, GHSA-46jr-4wx8-v453); 0.0.76 bundled 1.29.0. Its core asks for chromium 1252;
#   like 0.0.76 (asked 1226, ran the v1.61.1 image's 1228) it runs the image's nearest newer build.
# v3 (2026-11-10): driver-facing PORT = autoshot proxy ($B/proxy/autoshot-proxy.mjs): forwards all tools and
#   appends one viewport jpeg after each action tool (also saved to $OUT/auto/<session>/). Raw playwright MCP
#   listens on PORT+1000. GET http://127.0.0.1:PORT/status -> active sessions (used by clean-pwmcp.sh).
#   NO_PROXY=1 run-pwmcp.sh ... = v2 layout (playwright directly on PORT, no auto screenshots).
# Bound to 127.0.0.1 only. Rollback: docker rm -f <driver>-pwmcp ; previous script: run-pwmcp.sh.bak-20261110c
set -euo pipefail
B=/home/linxuhao/.AItelier/browser
PKG=$B/pkg-0.0.84
DRIVER=${1:?driver name}; PORT=${2:?port}
EXTRA_FLAGS=${EXTRA_FLAGS:-}
if [ "$DRIVER" = dsh ]; then D=$B; PROFILE=$B/profile; OUT=$B/out; HOMED=$B/home
else D=$B/drivers/$DRIVER; PROFILE=$D/profile; OUT=$D/out; HOMED=$D/home; fi
mkdir -p "$PROFILE" "$OUT" "$HOMED"
if [ "${NO_PROXY:-0}" = 1 ]; then BPORT=$PORT; else BPORT=$((PORT+1000)); fi
PW="node $PKG/node_modules/@playwright/mcp/cli.js \
 --headless --browser chromium --no-sandbox \
 --executable-path /ms-playwright/chromium-1254/chrome-linux64/chrome \
 --host 127.0.0.1 --port $BPORT --allowed-hosts 127.0.0.1:$BPORT,localhost:$BPORT \
 --caps vision --viewport-size 1280x720 --image-responses allow \
 --allowed-origins https://linxuhao.github.io\;http://127.0.0.1:4444\;http://localhost:4444\;http://127.0.0.1:5173 \
 --user-data-dir $PROFILE --output-dir $OUT --save-session --snapshot-mode none \
 --config $B/pwmcp-config.json $EXTRA_FLAGS"
if [ "${NO_PROXY:-0}" = 1 ]; then CMD="exec $PW"
else CMD="$PW & LISTEN_PORT=$PORT BACKEND_URL=http://127.0.0.1:$BPORT/mcp AUTO_DIR=$OUT/auto node $B/proxy/autoshot-proxy.mjs & wait -n; exit 1"; fi
docker rm -f "$DRIVER-pwmcp" >/dev/null 2>&1 || true
docker run -d --name "$DRIVER-pwmcp" --restart unless-stopped --network host --init --ipc=host \
  --log-opt max-size=5m --log-opt max-file=2 \
  --user "$(id -u):$(id -g)" -e HOME="$HOMED" -v $B:$B -w "$D" \
  mcr.microsoft.com/playwright:v1.65.0-noble bash -c "$CMD"
echo "$DRIVER-pwmcp -> http://127.0.0.1:$PORT/mcp (backend $BPORT) profile=$PROFILE"
