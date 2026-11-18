#!/bin/sh
# Starts the WeChat bridge container (README.md). Nothing runs this for you: start it
# when the owner is ready to scan the login QR. Build the image first:
#   docker build -t aitelier-wechat-bridge:local integrations/wechat-bridge
#
# Only 127.0.0.1:${WECHAT_BRIDGE_PORT:-18741} on the host reaches it (inside, the bridge
# listens on 18731; 18741 because 127.0.0.1:18731 is taken on the owner's host; the
# channel plugin reads the same WECHAT_BRIDGE_PORT). The token file is the only host path it
# sees (read-only; the image runs as uid 1000, the owner of that file on this host);
# its root filesystem is read-only; the login lives in its memory, so
# a restart means a new QR. A QR nobody scans, a logout or a puppet failure before a
# login ends the process (exit 75); Docker starts it again at most 5 times, then it
# stays exited until `docker start aitelier-wechat-bridge`, so an unattended bridge does
# not keep asking WeChat for QRs.
set -eu

token="$HOME/.aitelier-secrets/wechat-bridge-token"
if [ ! -s "$token" ]; then
  echo "run.sh: no token in $token (one line, any random string); not starting" >&2
  exit 78
fi

exec docker run --detach --name aitelier-wechat-bridge \
  --restart on-failure:5 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 256 \
  --publish "127.0.0.1:${WECHAT_BRIDGE_PORT:-18741}:18731" \
  --volume "$token:/home/bridge/.aitelier-secrets/wechat-bridge-token:ro" \
  aitelier-wechat-bridge:local
