#!/usr/bin/env bash
# One Playwright MCP browser per driver: own container, port, profile (saves/IndexedDB), output dir.
# Usage: run-pwmcp.sh <driver> <port>      e.g. run-pwmcp.sh grok 8932 ; run-pwmcp.sh claude 8933
#   dsh keeps legacy layout (container dsh-pwmcp, port 8931, $B/profile, $B/out) via run-dsh-pwmcp.sh.
#   steward = the steward's own instance (design-steward 7.8.13): run-pwmcp.sh steward 8921, see below.
# Pinned: image mcr.microsoft.com/playwright:v1.65.0-noble (chromium-1254), @playwright/mcp@0.0.84
#   (shared install in $PKG = $B/pkg-0.0.84, from deploy/pwmcp/package-lock.json in the AItelier repo).
#   0.0.84 is the first @playwright/mcp whose playwright-core bundles @modelcontextprotocol/sdk 1.32.0
#   (GHSA-f897-62hf-6gxq, GHSA-46jr-4wx8-v453); 0.0.76 bundled 1.29.0. Its core asks for chromium 1252;
#   like 0.0.76 (asked 1226, ran the v1.61.1 image's 1228) it runs the image's nearest newer build.
# v3 (2026-11-10): driver-facing PORT = autoshot proxy ($B/proxy/autoshot-proxy.mjs): forwards all tools and
#   appends one viewport jpeg after each action tool (also saved to $OUT/auto/<session>/). Raw playwright MCP
#   listens on PORT+1000. GET http://127.0.0.1:PORT/status -> active sessions (used by clean-pwmcp.sh).
#   NO_PROXY=1 run-pwmcp.sh ... = v2 layout (playwright directly on PORT, no auto screenshots).
# Drivers: bound to 127.0.0.1 only. Rollback: docker rm -f <driver>-pwmcp ; previous script: run-pwmcp.sh.bak-20261110c
set -euo pipefail
# PWMCP_BROWSER_DIR only redirects the browser root for isolated tests; the default is unchanged.
B=${PWMCP_BROWSER_DIR:-/home/linxuhao/.AItelier/browser}
PKG=$B/pkg-0.0.84
DRIVER=${1:?driver name}; PORT=${2:?port}
EXTRA_FLAGS=${EXTRA_FLAGS:-}

# One iGPU detection for both arms. A render node counts as present by existence
# alone: the launching user has no render-group membership, so a host-side r/w
# probe would be false even though --group-add grants access in the container.
# PWMCP_GPU=off (exact) is the old SwiftShader path: same docker argv and base
# config as before, no device/group and no generated copy. Any other value = auto.
GPU_NODE=${PWMCP_DRI_DIR:-/dev/dri}/renderD128
gpu_setup() {  # $1 = the instance's own writable dir, mounted into the container at $1
  GPU_ARGS=(); GPU_CONFIG=$B/pwmcp-config.json
  if [ "${PWMCP_GPU:-auto}" = off ] || [ ! -c "$GPU_NODE" ]; then return 0; fi
  local copy=$1/pwmcp-config.gpu.json
  # The MCP CLI has no browser-argument switch: --config is the only route. Derive
  # a per-instance copy so the base config and its non-GPU args stay authoritative.
  python3 - "$B/pwmcp-config.json" "$copy" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    config = json.load(f)
args = config.setdefault("browser", {}).setdefault("launchOptions", {}).setdefault("args", [])
args.extend(("--use-angle=gl-egl", "--enable-gpu", "--ignore-gpu-blocklist"))
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PY
  GPU_CONFIG=$copy
  # -L: a symlinked render node must contribute its target's group, not the link's.
  GPU_ARGS=(--device "$GPU_NODE" --group-add "$(stat -Lc %g "$GPU_NODE")")
}
if [ "$DRIVER" = steward ]; then
  # The steward's own instance: same image, package and config as the drivers', its own container,
  # profile and port; the one client is the backend's steward gateway (a `browser` connection the
  # owner adds on the dashboard, private only). Differences from a driver's, each for that client:
  #   - on its own docker network (STEWARD_BROWSER_NETWORK, default steward-browser, created here
  #     when missing), not the host's: the gateway refuses loopback and host addresses, so it dials
  #     http://steward-pwmcp:PORT/mcp. Only steward-pwmcp and the backend (`aitelier`, which joins it
  #     in docker-compose.yml) are on it. Not aitelier_default: the browser listens on 0.0.0.0 with
  #     no auth (--allowed-hosts only checks the Host header), so any container sharing its network
  #     (aitelier-zg, aitelier-godot, ...) could drive it and skip the gateway's owner-only audience.
  #     Not --internal: the browser must reach the web. No published port. --shm-size instead of
  #     --ipc=host (no host namespace left).
  #   - no autoshot proxy (it listens on 127.0.0.1 only), playwright directly on PORT.
  #   - --caps vision: the coordinate input tools (browser_mouse_click_xy, ...). A canvas game has no
  #     element to click by ref, so seeing the screen is not enough to play it; measured on the
  #     steward-browser network, the same flags without --caps vision expose 25 tools and with it 31.
  #     Default text snapshots stay (the steward reads web pages through the snapshot), so no
  #     --snapshot-mode none: that is a drivers-only choice. --image-responses allow is what makes a
  #     screenshot taken WITHOUT a filename come back as an image; with a filename the server saves
  #     the file and answers with text only. No --allowed-origins (the steward browses the web), no
  #     --save-session logs.
  #   - profile (login state) under STEWARD_BROWSER_DIR, outside ~/.AItelier, which dev_mcp mounts.
  #     The package and config are mounted read-only.
  # Accepted (owner 2026-11-23): like the drivers' browser, its pages can reach the LAN (and the
  # backend, its one neighbour on steward-browser); nothing proxies or filters its traffic.
  D=${STEWARD_BROWSER_DIR:-$HOME/.local/share/aitelier/steward-browser}
  PROFILE=$D/profile; OUT=$D/out; HOMED=$D/home; NET=${STEWARD_BROWSER_NETWORK:-steward-browser}
  (umask 077; mkdir -p "$PROFILE" "$OUT" "$HOMED")
  gpu_setup "$D"
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  PW="node $PKG/node_modules/@playwright/mcp/cli.js \
 --headless --browser chromium --no-sandbox \
 --caps vision \
 --executable-path /ms-playwright/chromium-1254/chrome-linux64/chrome \
 --host 0.0.0.0 --port $PORT --allowed-hosts steward-pwmcp:$PORT \
 --viewport-size 1280x720 --image-responses allow \
 --user-data-dir $PROFILE --output-dir $OUT \
 --config $GPU_CONFIG $EXTRA_FLAGS"
  docker rm -f steward-pwmcp >/dev/null 2>&1 || true
  docker run -d --name steward-pwmcp --restart unless-stopped --network "$NET" --init --shm-size 1g \
    --log-opt max-size=5m --log-opt max-file=2 \
    --user "$(id -u):$(id -g)" -e HOME="$HOMED" -v "$PKG:$PKG:ro" -v "$B/pwmcp-config.json:$B/pwmcp-config.json:ro" \
    -v "$D:$D" -w "$D" "${GPU_ARGS[@]}" \
    mcr.microsoft.com/playwright:v1.65.0-noble bash -c "exec $PW"
  echo "steward-pwmcp -> http://steward-pwmcp:$PORT/mcp on $NET (no host port) profile=$PROFILE"
  exit 0
fi
if [ "$DRIVER" = dsh ]; then D=$B; PROFILE=$B/profile; OUT=$B/out; HOMED=$B/home
else D=$B/drivers/$DRIVER; PROFILE=$D/profile; OUT=$D/out; HOMED=$D/home; fi
mkdir -p "$PROFILE" "$OUT" "$HOMED"
gpu_setup "$D"
if [ "${NO_PROXY:-0}" = 1 ]; then BPORT=$PORT; else BPORT=$((PORT+1000)); fi
PW="node $PKG/node_modules/@playwright/mcp/cli.js \
 --headless --browser chromium --no-sandbox \
 --executable-path /ms-playwright/chromium-1254/chrome-linux64/chrome \
 --host 127.0.0.1 --port $BPORT --allowed-hosts 127.0.0.1:$BPORT,localhost:$BPORT \
 --caps vision --viewport-size 1280x720 --image-responses allow \
 --allowed-origins https://linxuhao.github.io\;http://127.0.0.1:4444\;http://localhost:4444\;http://127.0.0.1:5173 \
 --user-data-dir $PROFILE --output-dir $OUT --save-session --snapshot-mode none \
 --config $GPU_CONFIG $EXTRA_FLAGS"
if [ "${NO_PROXY:-0}" = 1 ]; then CMD="exec $PW"
else CMD="$PW & LISTEN_PORT=$PORT BACKEND_URL=http://127.0.0.1:$BPORT/mcp AUTO_DIR=$OUT/auto node $B/proxy/autoshot-proxy.mjs & wait -n; exit 1"; fi
docker rm -f "$DRIVER-pwmcp" >/dev/null 2>&1 || true
docker run -d --name "$DRIVER-pwmcp" --restart unless-stopped --network host --init --ipc=host \
  --log-opt max-size=5m --log-opt max-file=2 \
  --user "$(id -u):$(id -g)" -e HOME="$HOMED" -v $B:$B -w "$D" "${GPU_ARGS[@]}" \
  mcr.microsoft.com/playwright:v1.65.0-noble bash -c "$CMD"
echo "$DRIVER-pwmcp -> http://127.0.0.1:$PORT/mcp (backend $BPORT) profile=$PROFILE"
