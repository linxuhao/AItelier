# wechat-bridge

Keeps one WeChat login (Wechaty 1.20.2 + wechaty-puppet-neo 0.1.1) alive in its own
process and serves the account's 1:1 text messages to the steward's channel plugin
(`integrations/dsh-channel-wechat`) over loopback HTTP. Design: steward design §7.2.

Why a separate process: puppet-neo keeps the login in memory only, so a restart means
a new QR scan. Holding the login here means a steward or host restart does not log
WeChat out.

A login QR is asked of WeChat only on request, one at a time (owner, 2026-11-20: a
button or the steward's tool, never a manual container restart): `POST /login`, which
the channel plugin calls for the dashboard's "新二维码" button and for the steward's
`request_wechat_qr` tool. Inside the running process the bridge builds a fresh Wechaty
bot with a fresh puppet-neo for each request, which opens a new session and asks for a
new QR; an attempt that ends stops its own bot. No puppet state is shared between
attempts: a slow stop of an old bot never holds up the next one, and whatever an old bot
still emits (a late QR, a login, an error) is ignored. A QR nobody scans, a cancelled
one, a logout and a puppet failure before a login leave the bridge `idle`: still
running, no bot running, asking WeChat for nothing until the next request.
(OpenClaw's WeChat channel needs no long-running process at all: it uses Tencent's iLink
API and keeps the account token on disk. puppet-neo neither exports nor restores a login,
so this bridge must stay up - but it never needs a restart for a new QR.)

## Image

The bridge runs in its own container, built here (owner, 2026-11-18: not as a host
service):

```sh
docker build -t aitelier-wechat-bridge:local integrations/wechat-bridge
```

`Dockerfile`: the node binary from `node:24-bookworm-slim` (the stage AItelier's own
Dockerfile uses) on `python:3.12-slim` (Debian trixie, glibc 2.41; puppet-neo needs
glibc ≥ 2.38, bookworm has 2.36). npm exists only in the build stage, which runs
`npm ci --omit=dev --ignore-scripts` from the committed `package-lock.json` (exact
pins); the final image has node, the app and its `node_modules`, no npm and no
compilers, and runs as uid 1000 (`bridge`). The app's files are copied in with `COPY --chmod=0444`, so
the image does not depend on the checkout's file modes: built from a checkout whose files
are 0600 (this host's), a plain `COPY` made them root-owned 0600 and uid 1000 could not
read `main.mjs` (EACCES at start). Checked 2026-11-20: the same 0600 copy built with the
old Dockerfile exits with EACCES, with this one it listens.

`--ignore-scripts` is enough. puppet-neo has no install scripts: its native addon
(`native/<platform>/neo.node`, all four platforms, ~45 MB unpacked) comes inside its npm
tarball. In the dependency tree only these declare install-time scripts, and none is
needed: `@swc/core` postinstall (checks its native binary, which arrives as the npm
optional dependency `@swc/core-linux-x64-gnu`, and would otherwise fetch `@swc/wasm`
from npm), `leveldown` install (`node-gyp-build`, which only compiles when no prebuild
fits; `prebuilds/linux-x64` ships in the package) and `protobufjs` postinstall (a
version-scheme warning). The rest are `prepare` scripts, which npm does not run for
registry installs.

### The puppet is a fork

The npm tarball's addon and its compiled JavaScript are replaced by a fork of
wechaty-puppet-neo: `neo-fork/pin.json` names the upstream commit and the sha256 of every
file the patch touches, `neo-fork/0001-incoming-message-types.patch` is the change, and
the Dockerfile's `native` stage (Rust 1.93.1, the pin's `rust-toolchain.toml` channel)
fetches that commit, verifies those blobs, applies the patch, runs `cargo build --release
--locked` and rebuilds `dist/` with `tsc`, then runs the package's own tests. The build
stage overlays exactly two paths from it - `node_modules/wechaty-puppet-neo/native/linux-x64/neo.node`
and `node_modules/wechaty-puppet-neo/dist/` - before the load test above, so the image's
own check runs the fork. The fork's source is not committed here; the upstream files the
patch touches are vendored under `neo-fork/upstream/` (Apache-2.0, see its `NOTICE`) so
`tests/unit/test_wechat_bridge_neo_fork.py` can check the pin, the patch and the
Dockerfile offline.

Why: 0.1.1's native addon dropped every incoming message whose type was not text (image
3, voice 34, video 43, sticker 47, app messages and quoted replies 49/57, system notices
10000) before any JavaScript could see it. The fork's native layer is faithful - it drops
`outgoing` only and reports WeChat's own type with the raw `content`/`inline_data` bytes -
and which arriving types may enter a conversation is the consumer's decision: this bridge
serves text and, since step 24.2, 1:1 images (3), voices (34) and files (49 with appmsg type
6); quoted replies (49/57), system notices and other app messages stay out of conversations
until a rule says otherwise.

What the build checks, and what it found (2026-11-18, log in
`~/.AItelier/artifacts/aitelier/dsh-director-plugin-20261114/s19-wechat-image-build.txt`):

- `ldd` on `neo.node`: it needs only `libgcc_s.so.1`, `libm.so.6`, `libc.so.6` and
  `ld-linux-x86-64.so.2`, all resolved (OpenSSL and the rest are linked in
  statically); its newest glibc symbol version is `GLIBC_2.38`; the image has 2.41.
- puppet-neo 0.1.1 declares `engines: node >=26`; the image's node is v24.22.3 (npm
  prints `EBADENGINE` and goes on). Its JavaScript uses nothing newer than node 24, and
  the build loads `wechaty` and `wechaty-puppet-neo` and opens and closes a native
  session the way the puppet does (`new PuppetNeo().createSession()`), twice, then
  builds, starts and stops a fresh Wechaty bot with a fresh puppet twice in the same
  process, with no network (`RUN --network=none`): each start opens a fresh session and
  tries a new QR login (which fails offline, as it should), and each stop closes it -
  what `POST /login` does. It works on node 24. A real login was not tried.

## Run

Secret: `$HOME/.aitelier-secrets/wechat-bridge-token` (one line, any random string, owned
by uid 1000, mode 0600). The same file is read by the channel plugin
(`integrations/dsh-channel-wechat`). The directory is HOME-relative and fixed: no env var
moves it (`AITELIER_SECRETS_DIR` does not). Without it the bridge does not start (exit
78) and the plugin stays disabled.

The steward's profile loads the channel plugin only when the token file exists, checked
when the profile is written, at the steward's start: a token added to a running steward
loads nothing until the steward is restarted. Until then the dashboard's Channels tab
shows no status for WeChat at all (no QR to scan either; if it ran before, its last
report); after the restart it shows the bridge's state (`no QR (<reason>) - request one
on the dashboard`, `QR requested`, `scan needed` with the QR, `QR scanned - confirm on the
phone`, `connected`, or why not).

`run.sh` starts the container (nothing runs it for you):

- non-root (uid 1000), `--read-only` with a 32 MiB tmpfs on `/tmp` (one fetched media
  file at a time, step 24.2), `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--memory 512m --cpus 1 --pids-limit 256`;
- publishes only `127.0.0.1:18741:18731` (host port `WECHAT_BRIDGE_PORT`, default
  18741, the same variable and default the channel plugin uses); inside, the bridge
  listens on port 18731 on `0.0.0.0`
  (`WECHAT_BRIDGE_BIND`, set by the image; the bridge accepts `0.0.0.0` only inside a
  container and otherwise listens on `127.0.0.1`);
- on its own Docker network, `aitelier-wechat-bridge` (`run.sh` creates it when it is
  missing), not Docker's default bridge: inside, the bridge listens on `0.0.0.0`, and on
  the default bridge every other container on `docker0` could reach it with only the
  token in the way. No other container shares its network. Processes on the host can
  still reach the container's own address on that network; the token guards that;
- the token file read-only is the only host mount; outbound network is allowed (WeChat's
  servers);
- `--restart unless-stopped`: a start is `idle` and asks WeChat for nothing, so a
  restart (a crash, a host reboot) is harmless and the bridge is back to wait for a
  request. (`on-failure:N` would not bring it back after a host reboot. The cost: a
  missing token or a refused bind address, exit 78, restarts under Docker's backoff
  forever - log lines only, WeChat is never asked.)

Then ask for a QR (the dashboard's Channels tab, "新二维码", or ask the steward) and scan
it: `GET /status` carries it as `qr` and drawn as an SVG image as `qr_svg` (qr-svg.mjs,
with the QR encoder qrcode-terminal ships, a package puppet-neo already pins); the
channel plugin reports the image with its status, and the owner scans it on the
dashboard's Channels tab - only there: the QR never goes over a chat channel. The QR is a
live login token for this bridge: whatever shows or logs that status must not log it.
The process ends only on SIGINT/SIGTERM (0), a missing token or a refused bind address
(78), or a crash; exit 75 is no longer used. The login lives in the container's memory
only: a restart means a new scan. Wechaty keeps no memory-card file (the bot has no
`name`), so nothing is written to disk.

The host port is 18741 and not the container's 18731 because on this host
`127.0.0.1:18731` is taken by an unrelated `python3 -m http.server 18731 --bind
127.0.0.1` (running since 2026-11-06). To use another one, set `WECHAT_BRIDGE_PORT`
for both `run.sh` and the steward's host (or the plugin's `url` config).

Start check without a login: `WECHAT_BRIDGE_SKIP_LOGIN=1` serves the API and never
builds a bot (`POST /login` answers `409 login_disabled`). With the `run.sh` flags, a throwaway token and host port 18741, on
2026-11-18 `GET /status` answered `200 {"logged_in":false,...}`, without the token `401`,
`/events` `200`, `/send` `409 not_logged_in`, and the root filesystem refused writes
(log: `s19-wechat-image-startcheck.txt` next to the build log). The same check of
`run.sh` itself with its own network (host port 18753, `WECHAT_BRIDGE_SKIP_LOGIN=1` added
to `docker run` by a stand-in `docker`): the container was on `aitelier-wechat-bridge`
only, `/status` answered `200` with the token and `401` without, a container on the
default bridge could not connect to the bridge's address, and a second start reused the
network (log: `s19-sf-b-bridge-smoke.txt`).

## API

Listens on `127.0.0.1` (or `0.0.0.0` inside its container, published on the host's
`127.0.0.1:18741` only), port `WECHAT_BRIDGE_PORT`, default 18731 (the image sets
18731; on the host side `WECHAT_BRIDGE_PORT` is the published port, default 18741).
Every request
needs `Authorization: Bearer <token>`; anything else is `401 {error: "unauthorized"}`.

- `GET /status` → `{phase, idle_reason?, logged_in, user_id?, qr?, qr_svg?, since}`.
  `phase`: `idle` (no QR, not logged in, no bot running; `idle_reason` is `boot`,
  `qr_expired`, `qr_cancelled`, `logged_out` or `login_failed`), `starting` (a QR was
  requested and has not appeared yet), `waiting_qr`, `scanned` (to be confirmed on the
  phone) or `logged_in`. `qr` (the payload) and `qr_svg` (it drawn as an SVG, absent if
  it cannot be drawn within 64 KiB) only in `waiting_qr`; `since` is when the current
  phase began.
- `POST /login` (no body) → `202 {phase: "starting"}`: build and start a fresh bot, and
  a new QR follows in `/status` (usually within seconds). Taken only while `idle`;
  otherwise it changes nothing and answers `409 {refused}`: `already_logged_in` (log out
  on the phone first to change accounts), `qr_waiting` (a QR is waiting or was just
  scanned: scan that one), `login_starting` (the last request has not shown its QR yet),
  `login_disabled` (`WECHAT_BRIDGE_SKIP_LOGIN=1`); or `429 {refused: "rate_limited",
  retry_after_s}`: at least 2 minutes between two accepted requests, at most 5 in any
  hour and 12 in any 24 hours (`LOGIN_LIMITS`, kept in memory; the director's
  `ownerChannels.login` keeps the same count, so a bridge restart does not reset it
  there). A bot that cannot be built or started, or a QR that has not appeared within 30
  seconds, ends as `idle` (`login_failed`); a QR still waiting or scanned 5 minutes after
  it appeared ends as `idle` (`qr_expired`) whatever the puppet still does (neo's own
  loop ends in about 2 minutes). The reason is logged (at most 200 characters, never a
  QR).
- `GET /events?after=<n>&wait=<sec>` → `{events: [{seq, type: "message", sender_ref,
  message_ref, text, at}], next_after}`. Long-poll: waits up to `wait` seconds
  (capped at 25) for an event after `after`. Only 1:1 text messages from another
  contact: not the account's own, not rooms, not official accounts (`gh_` ids). Kept in
  memory, the last 500. Sequence numbers start at the boot time in milliseconds, so a
  cursor kept from before a bridge restart never hides newer events. `at` is when the
  owner wrote it, as WeChat dates the message; a message WeChat does not date has no
  `at` (the host then refuses it as an approval, `approval_undated`, and still writes it).
- `POST /send {to, text}` → `{ref}`. Text longer than 1500 characters goes as
  consecutive messages; `ref` is the first part's message id, or `null` when the puppet
  returns none (puppet-neo does not). Refusals: `409 not_logged_in`, `409
  recipient_unknown` (puppet-neo can only write to a peer that has written to this
  login since it started, and only the last 2048 of them), `400 bad_request`, `413
  body_too_large` (over 64 KiB), `502 send_failed`.

Step 24.2 media (design-steward 7.8.12 "微信"; the fork's interface as the platform
director gave it: `messageFile`/`messageImage`/`messageVoice` → FileBox, a voice as WAV;
`messageSendFile(conversationId, fileBox)` with the kind taken from the FileBox name):

- A 1:1 image, voice or file is an event with `text: ""` and `attachments: [{media_id,
  kind, mime?, name?, size?}]` - a description only, nothing is fetched on arrival. Over the
  size WeChat reports (image 20 MiB, file 25 MiB, the voice's SILK 2 MiB): `too_large:
  true` and no `media_id`. In the stranger buffer (`owner_ref` set, another sender):
  `gone: true` and no `media_id`. Group messages stay text only. A file (49) is let in only
  when its appmsg XML is in the payload and says type 6 with no quote; the fork filing 49
  under Wechaty's `Unknown` is accepted by the raw type.
- `GET /media/<media_id>` (`<seq>.<index>`; anything else is `404`) → the bytes (a voice as
  `audio/wav`), fetched from WeChat now and held in `/tmp` only while answering. `404
  not_found` (its event left the ring, or never was), `410 gone` (another login's message,
  a puppet refusal the bridge recognises - `observed peer`, `expired`, `not found` - or the
  5th failed fetch of the same media_id), `503 busy` (another fetch is running), `503
  unavailable` (any other failure: the puppet, `/tmp`, no first byte in 30 s, not whole in
  120 s, or the client went away - the lock is released and the partial deleted), `413
  too_large` (more bytes than the bound; a voice over 2 min 5 s of WAV by its header's rate).
- `POST /send-file?to=<wxid>|room=<room>&kind=image|voice|file&mime=&name=` with the
  file's bytes as the body (at most 25 MiB, `413 body_too_large`) → `{ref, sent_as}`. An
  image goes under an image extension, a file under its own name (`.bin` added when that
  name would read as an image or a voice); a voice that is a WAV first as `<stem>.silk` (a
  voice bar), and when that is refused or fails, the same bytes again under its own name as
  a file (`sent_as: "file"`); a voice in any other format goes as a file at once. Group sends share the group queue and its backstop.

Message texts are never logged.

## Accepted risks

- Approval power over chat rests on the steward host's driver token. A process running
  as the same user that holds it could relay an approval phrase in-protocol with false
  attribution (recorded as decided by the owner on a channel). Under the current
  same-user trust model this is no new boundary: such a process could already write
  `aitelier.db`. A later hardening is a separate, host-only credential for the channel
  commands.
- A stranger who can message the bot can retire a live binding code with 5 wrong
  guesses. It cannot take the binding over: the code is stored only as an HMAC keyed
  with the driver pepper secret file (like the driver tokens), so a copy of the State
  database does not reveal it - without the pepper no code is issued or checked
  (`channel_secret_missing`) - and online guessing stays bounded at 5 guesses in 10^6.
  The refusals are in the log with the sender, and the owner just issues a new code.
- Token files (this bridge's, the Slack tokens) should be mode 0600; nothing enforces it.

## Tests

```sh
node --test bridge.test.mjs qr-svg.test.mjs
```

Fake Wechaty bots; nothing loads the puppet or reaches WeChat. Fresh real bots started
and stopped in one process are checked offline by the image build (above).
