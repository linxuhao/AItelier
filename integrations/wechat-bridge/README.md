# wechat-bridge

Keeps one WeChat login (Wechaty 1.20.2 + wechaty-puppet-neo 0.1.1) alive in its own
process and serves the account's 1:1 text messages to the steward's channel plugin
(`integrations/dsh-channel-wechat`) over loopback HTTP. Design: steward design §7.2.

Why a separate process: puppet-neo keeps the login in memory only, so a restart means
a new QR scan. Holding the login here means a steward or host restart does not log
WeChat out.

## Install

```sh
cd integrations/wechat-bridge
npm ci --ignore-scripts
```

`--ignore-scripts` is enough. puppet-neo has no install scripts: its native addon
(`native/<platform>/neo.node`, all four platforms, ~45 MB unpacked) comes inside its npm
tarball. In the dependency tree only these declare install-time scripts, and none is
needed: `@swc/core` postinstall (checks its native binary, which arrives as the npm
optional dependency `@swc/core-linux-x64-gnu`, and would otherwise fetch `@swc/wasm`
from npm), `leveldown` install (`node-gyp-build`, which only compiles when no prebuild
fits; `prebuilds/linux-x64` ships in the package) and `protobufjs` postinstall (a
version-scheme warning). The rest are `prepare` scripts, which npm does not run for
registry installs.

Requirements of puppet-neo: Linux x64 with glibc ≥ 2.38 (this host: 2.39) and, per its
`engines`, Node ≥ 26.

## Run

Secret: `~/.aitelier-secrets/wechat-bridge-token` (one line, any random string). The
same file is read by the channel plugin. Without it the bridge does not start (exit 78)
and the plugin stays disabled.

As a systemd user unit (`aitelier-wechat-bridge.service`, not installed by anything):

```sh
cp aitelier-wechat-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start aitelier-wechat-bridge
```

Then scan the QR: `GET /status` carries it as `qr`, and the channel plugin reports it in
its status detail. A QR nobody scans, or a logout, ends the process so the next start
shows a fresh QR; the unit allows five starts an hour.

Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` with the state dir
(`~/.local/state/aitelier-wechat-bridge`, also the working directory, where Wechaty
writes its memory card) as the only writable path, and `RestrictAddressFamilies=AF_INET
AF_INET6 AF_UNIX`.

Not in a container, for now: puppet-neo's prebuilt addon needs glibc ≥ 2.38 and no
local image has it; pulling one needs the owner's approval.

## API

Listens on `127.0.0.1` only (port `WECHAT_BRIDGE_PORT`, default 18731). Every request
needs `Authorization: Bearer <token>`; anything else is `401 {error: "unauthorized"}`.

- `GET /status` → `{logged_in, user_id?, qr?, since}`. `qr` only while a QR can be
  scanned; `since` is when the current login state began.
- `GET /events?after=<n>&wait=<sec>` → `{events: [{seq, type: "message", sender_ref,
  message_ref, text, at}], next_after}`. Long-poll: waits up to `wait` seconds
  (capped at 25) for an event after `after`. Only 1:1 text messages from another
  contact: not the account's own, not rooms, not official accounts (`gh_` ids). Kept in
  memory, the last 500. Sequence numbers start at the boot time in milliseconds, so a
  cursor kept from before a bridge restart never hides newer events.
- `POST /send {to, text}` → `{ref}`. Text longer than 1500 characters goes as
  consecutive messages; `ref` is the first part's message id, or `null` when the puppet
  returns none (puppet-neo does not). Refusals: `409 not_logged_in`, `409
  recipient_unknown` (puppet-neo can only write to a peer that has written to this
  login since it started), `400 bad_request`, `502 send_failed`.

Message texts are never logged.

## Tests

```sh
node --test bridge.test.mjs
```

A fake Wechaty; nothing loads the puppet or reaches WeChat.
