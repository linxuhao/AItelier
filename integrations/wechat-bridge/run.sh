#!/bin/sh
# Starts the WeChat bridge container (README.md). Nothing runs this for you. Build the
# image first:
#   docker build -t aitelier-wechat-bridge:local integrations/wechat-bridge
#
# Only 127.0.0.1:${WECHAT_BRIDGE_PORT:-18741} on the host reaches it (inside, the bridge
# listens on 18731; 18741 because 127.0.0.1:18731 is taken on the owner's host; the
# channel plugin reads the same WECHAT_BRIDGE_PORT). The image runs as uid 1000, the owner
# of the host paths it sees; its root filesystem is read-only. The bridge asks WeChat for a
# QR only when one is requested (POST /login: the dashboard's button or the steward's tool);
# a QR nobody scans, a logout or a puppet failure before a login leave it idle, still
# running.
#
# Step 19.1 (design-steward 7.2.1; the owner's rule of 2026-11-22: a start resumes by
# itself, with nothing to resume it waits for a QR to be asked for). The puppet keeps the
# login encrypted on disk, and a start (also Docker's restart after a crash or a host
# reboot: unless-stopped) that finds one resumes it - no QR; with none it is idle and asks
# WeChat for nothing. This script creates, outside ~/.aitelier-secrets (which the backend
# container mounts whole):
#   ~/.local/share/aitelier/wechat-bridge-key/key   32 random bytes, 0600, made once when
#       missing and never printed; mounted read-only as the file /home/bridge/saved-login-key
#   ~/.local/share/aitelier/wechat-bridge-state/data/   0700; mounted read-write as the
#       directory /home/bridge/state (a directory, so the puppet's rename is atomic)
# and hands the puppet the two paths (PUPPET_NEO_STATE_DIR, PUPPET_NEO_STATE_KEY_FILE).
# Both parent directories are 0700. The token file is mounted read-only as before.
#
# The off switch: WECHAT_BRIDGE_SAVE_LOGIN=0 mounts neither and sets neither, which is the
# bridge as it was before 19.1 (the login in memory only, a restart means a new QR), and
# deletes the saved login and the recorded account (data/account.v1, data/account.user and
# any temporary files of theirs; only deleted, never read). data/resumes (resume times, not
# secret) and the key stay, so the resume cap still holds when it is turned on again. A
# local delete does not sign the device out of WeChat: to end it there too, log this
# device out on the phone. No image rebuild is needed either way.
#
# It runs on its own Docker network (aitelier-wechat-bridge, created here if missing),
# not Docker's default bridge: inside, the bridge listens on 0.0.0.0, and on the
# default bridge every other container on docker0 could reach it, the token being
# the only guard. On its own network no other container shares its segment.
#
# /tmp is a 32 MiB tmpfs (step 24.2): GET /media/:media_id holds one fetched file there while
# it answers (at most 25 MiB, one at a time) and deletes it after.
set -eu

token="$HOME/.aitelier-secrets/wechat-bridge-token"
if [ ! -s "$token" ]; then
  echo "run.sh: no token in $token (one line, any random string); not starting" >&2
  exit 78
fi

state="$HOME/.local/share/aitelier/wechat-bridge-state"
key="$HOME/.local/share/aitelier/wechat-bridge-key"
case "${WECHAT_BRIDGE_SAVE_LOGIN:-1}" in
  0)
    rm -f "$state/data/account.v1" "$state/data/account.user" \
      "$state"/data/account.v1.tmp-* "$state"/data/account.user.tmp-*
    set --
    ;;
  1)
    umask 077
    mkdir -p "$state/data" "$key"
    chmod 0700 "$state" "$state/data" "$key"
    if [ ! -s "$key/key" ]; then
      head -c 32 /dev/urandom > "$key/key.tmp"
      mv "$key/key.tmp" "$key/key"
    fi
    chmod 0600 "$key/key"
    set -- --volume "$key/key:/home/bridge/saved-login-key:ro" \
      --volume "$state/data:/home/bridge/state:rw" \
      --env PUPPET_NEO_STATE_DIR=/home/bridge/state \
      --env PUPPET_NEO_STATE_KEY_FILE=/home/bridge/saved-login-key
    ;;
  *)
    echo "run.sh: WECHAT_BRIDGE_SAVE_LOGIN must be 0 or 1; not starting" >&2
    exit 78
    ;;
esac

network=aitelier-wechat-bridge
docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null

exec docker run --detach --name aitelier-wechat-bridge \
  --network "$network" \
  --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 256 \
  --publish "127.0.0.1:${WECHAT_BRIDGE_PORT:-18741}:18731" \
  --volume "$token:/home/bridge/.aitelier-secrets/wechat-bridge-token:ro" \
  "$@" \
  aitelier-wechat-bridge:local
