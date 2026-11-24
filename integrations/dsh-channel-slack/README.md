# dsh-channel-slack

The owner's Slack direct messages as an owner channel, and the private channels the owner
binds as steward groups: a DSH plugin that registers the channel `slack` with the
`ownerChannels` service (provided by dsh-director when its host declares the `channels`
capability). Design: step 19, §7.2; groups: step 23, §7.7.19 (the WeChat group design with
only this platform layer changed).

## What it does

- A person's direct message to the bot (`message` event, `channel_type: im`, no subtype,
  not from a bot) → `ownerChannels.inbound({channel: 'slack', senderRef: <user id>,
  messageRef: <client_msg_id or ts>, text, at})`. Every Socket Mode envelope is
  acknowledged, whatever it is. Whether the sender is the bound owner is the service's
  decision, not this plugin's.
- Slack never redelivers an acknowledged envelope, so the plugin keeps the message: while
  the service answers a transient reason - `unavailable` (the host or the backend cannot be
  reached), `rate_limited` (the channel's minute's share is spent: a replay after an outage
  is a burst by nature) or `approval_pending` (the decision this message carries is still
  running) - or throws, the message is held and handed in again under the same `messageRef`
  (which the host takes once), 5 s doubling to at most 60 s between attempts, until it is
  accepted or refused for good or the plugin stops. Each sender's messages stay in order (a
  held one is not overtaken by the next); other senders are not held up. At most 100
  messages per sender are held in memory (past that one is logged by sender and length and
  not held). A message whose answer never came back, or whose
  decision's line could not be sent, is also written to the director's owed store
  (`$AITELIER_DSH_STATE_DIR/channel-owed.json`, mode 0600; the owner's own text, like the
  State database that takes it) and asked again after a restart, in the order it arrived;
  the entry is cleared only once it has been answered for good - a line that could not be
  sent is owed, not lost. The fact of the debt is written before any host call; the text
  joins it only once the host has said that text may be kept (`channels.codeCheck`, which
  answers with the relay's own rule: a bound sender, and no live binding code anywhere in
  the text in any rendering the backend folds). A client-side digit rule cannot replace
  that answer - six digits in a row would silence an approval whose proposal id carries
  one, and the folded renderings it misses leave the live code on disk - so the plugin asks
  and, until the answer, keeps the message without its text: a stop in that window costs
  the sender one "send it again" line, never a live code on disk. Without a State dir the
  owe lives in memory only, so a restart loses it.
- Files (step 24.3, design-steward §7.8.12): a DM with files - subtype `file_share` or no
  subtype, both still sent by Slack - is taken the same way (a bot's never). Before
  `inbound`, each of its first 10 files goes through `ownerChannels.storeMedia`, which checks
  that the sender is the bound account BEFORE asking for a byte (a stranger's file is never
  fetched; the message then goes in without attachments, so no placeholder line either). The
  plugin itself refuses, never fetching: a remote file (`is_external`, another service's), a
  deleted one (`gone`), a download address that is not `https://files.slack.com/...` on the
  default port (`team.slack.com`, any other `*.slack.com` host and `files.slack.com.<else>`
  included), and a declared size over the media library's bound for its kind (`too_large`;
  the bounds are `integrations/dsh-director/media-types.json`, the library's own table). Such
  a file goes in without storeMedia, except the message's first file, which always goes
  through storeMedia (with an `open` that throws the refusal) so the host checks the sender
  even when every file was refused here. A download carries the bot token, follows at most 3
  redirects and only to `files.slack.com` (a 4th is refused `unsupported`), and treats
  404/410 or an empty success (204, no body) as `gone`, 401/403 or an HTML page (what Slack
  serves when the app lacks `files:read`) as `unsupported`, anything else (429, 5xx, the
  network) as transient: the message is held and the file fetched again. A redirect to any
  other host - another Slack host such as `slack-files.com` or `<team>.slack.com` too - is
  not followed and the file is refused `unsupported` (logged `redirected off
  files.slack.com`): fail-closed on purpose, so the bot token is only ever sent to
  `files.slack.com`. Should Slack start serving downloads from another host, files show as
  not received until this plugin's `slackFileUrl` admits that host. The real type is decided
  by the library from the first bytes; the placeholder lines are the host's. Logs carry no
  file name.
- `sendFile({to, kind, name, path, caption?})` → uploads one file from the host's library to
  the DM of `to` with the SDK's `filesUploadV2` (`files.getUploadURLExternal`, the upload,
  `files.completeUploadExternal`; the old `files.upload` was retired 2025-11-12), the caption
  as its initial comment; at most the library's outbound bound (25 MiB). Kinds: image, voice,
  file. With a `groupRef` instead of `to` it uploads into that bound group's channel (the
  same checks as `sendGroup` below). The ref is the file id.
- `send(text, {replyToRef, to})` → posts to the DM of `to`, the account the service has
  bound (it re-reads the binding and passes it on every send). The plugin keeps no idea
  of its own about who the owner is; without `to` the send fails. Text over 3500
  characters is sent as consecutive messages; the first message's `ts` is the ref.
- `status()` → `{connected}` from the socket's own state events, with a `detail` when not
  connected (`connecting`, `disconnected`, `disconnected: <slack error>` such as
  `invalid_auth`, or why it is not configured).
- Private channels as steward groups (step 23, design-steward §7.7.19). The only channel
  event the app subscribes to is `app_mention`: a channel message that does not mention the
  bot is never sent to the app (D6), so un-mentioned files are out of scope too (a Slack
  group has no media switch; the backend refuses one, `channel_no_kept_media`). The host
  hands the plugin its whitelist (`setRooms`: the bound account and each bound group, kept in
  memory; until the first list arrives a mention waits). A mention in a bound channel goes to
  `inbound` as a group message - `conversationRef` the channel id, `messageRef`
  `<channel>:<ts>` (a `ts` is unique only within its channel), the bot's own mention taken
  out of the text, `asked: true`, its files through `storeMedia` with the channel - on its
  own lane per channel, never holding up a DM. In any other conversation a mention goes in
  only when the bound account wrote nothing but a group code (`@AItelier Steward G-XXXX-XXXX`),
  as a bind with the bot's mention `<@U...>` as the group's prefix; anything else is dropped,
  unanswered and with no text logged. What the backend does with either is exactly what it
  does for a WeChat group (who may ask, the quotas of D11 - 60 questions per group and hour,
  20 per person - the turn audience, guards, storage and family memory).
- A group is a **private channel shared with no other organization**: before a group code is
  taken, and again on every message and every send of a bound group, `conversations.info`
  must say `is_private`, not `is_im`/`is_mpim`, and not `is_ext_shared`/`is_pending_ext_shared`
  (D4 (a), D5 (a)); the envelope's own `is_ext_shared_channel` is checked first. A code posted
  in a public channel, a Slack Connect channel, a DM or a group DM binds nothing (logged
  `a group code in <channel> not taken: <why>`). A bound channel found shared or public is
  **paused**: its events are dropped and nothing is sent into it (`room_paused`) for the rest
  of the process, and the channel's status says `n group(s) paused (...): unbind on the
  dashboard`; unbinding it lifts the pause. A restart judges it again on its next message or
  send.
- `sendGroup(channel, text, {replyToRef?})` → posts into a bound group: under the thread of
  the message it answers (`replyToRef`, a group `messageRef`; a question asked inside a
  thread is answered in that thread, D8), else into the channel (a reminder); `unfurl_links`
  and `unfurl_media` false; escaped like every text except the bot's own mention (the
  group's prefix, which a confirmation line asks people to write). The backend gives a Slack
  reply no WeChat quote line. A channel not bound is refused `room_not_bound`, a paused one
  `room_paused`, both before anything is posted.
- Reconnects itself: the SDK's own reconnect is turned off (`autoReconnectEnabled: false`)
  because in @slack/socket-mode 3.1.1 a reconnect that Slack refuses (a revoked token,
  for instance) leaves a rejected promise nobody handles, which would end the steward's
  process. The plugin retries every failed or dropped connection itself, 1 s doubling to
  at most 5 min, and stops on dispose.

## Setup

One Slack app per steward, and one running steward per app: Slack spreads a Socket Mode
app's events over all of its open connections, so a second process would receive part of
the owner's messages.

### A. From the manifest (recommended)

[`slack-app-manifest.json`](slack-app-manifest.json) describes exactly the app this
plugin needs: Socket Mode on, no Request URL, the bot scopes `app_mentions:read` (mentions
in the bound channels), `chat:write`, `files:read` (downloading files), `files:write`
(sending files), `groups:read` (`conversations.info`: is a channel private, is it shared),
`im:history`, `im:write`, the two bot events `message.im` and `app_mention`, and a Messages
tab people can write in. No `chat:write.public`, no `mpim:*`, no `channels:*` and no
history scope of any channel. It holds no token and no URL.

1. **Create the app.** At <https://api.slack.com/apps>: **Create New App → From a
   manifest**, pick the workspace, choose the **JSON** tab, replace its content with the
   whole of `slack-app-manifest.json`, then **Next → Create**.
2. **App-level token.** **Basic Information → App-Level Tokens → Generate Token and
   Scopes**: any name, scope `connections:write`, **Generate**. Copy the token (starts
   `xapp-`).
3. **Install.** **Install App → Install to Workspace → Allow**. Copy the **Bot User OAuth
   Token** (starts `xoxb-`).

Then save the two tokens, in **bash** (on macOS, whose shell is zsh, type `bash` first:
zsh's `read -p` means something else, and plain `sh` has no `read -s`). Paste each token
at its prompt: `read -s` does not echo it, so it never reaches the screen, the shell
history or a chat. A value that is empty or lacks the expected prefix is not written:

```bash
mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets
save_token() { local t; read -rsp "$1 token: " t; echo
  case "$t" in "$1"?*) ;; *) echo "not saved: not an $1 token"; return 1 ;; esac
  ( umask 077; printf '%s\n' "$t" > ~/.aitelier-secrets/"$2" ) && chmod 600 ~/.aitelier-secrets/"$2"; }
save_token xapp- slack-app-token
save_token xoxb- slack-bot-token
```

Then **restart the steward** (see below), open the app in Slack (**Apps → AItelier
Steward**, Messages tab) and write to it.

### B. By hand

Create an app **From scratch**, then: enable **Socket Mode** and make an app-level token
with `connections:write` (`xapp-`); bot token scopes `app_mentions:read`, `chat:write`,
`files:read`, `files:write`, `groups:read`, `im:history`, `im:write`; **Event
Subscriptions** on, bot events `message.im` and `app_mention`, no Request URL; **App
Home → Messages Tab** on, with "Allow users to send Slash commands and messages from the
messages tab" ticked; do not turn on **Agents & AI Apps**. Install it to the workspace
(`xoxb-`), then save the tokens and restart as in A.

### Fixing an existing app

An app made from a template (Slack's bolt starter agent, for instance) can carry an
Event Subscriptions Request URL (an ngrok address) with Socket Mode off, so Slack sends
the DM events to that address instead of the socket and nothing reaches the steward. It
can also carry the **Agents & AI Apps** feature, which this plugin does not use: Slack
then shows the DM as threads, while the plugin replies at the top of the DM, not in the
thread the person wrote in. Open the app's **App Manifest** page, replace the whole
manifest with `slack-app-manifest.json` (JSON tab), and **Save Changes**: a saved
manifest replaces the app's whole configuration, so that turns Socket Mode on and
removes the Request URL, the AI feature and any scope or event the plugin does not use.
It also renames the app and its bot to "AItelier Steward" (edit `name` and
`display_name` before pasting to keep yours). If Slack then asks to reinstall the app, do
so; if the Bot User OAuth Token shown afterwards differs from the saved one, save only it
again: run the `save_token` definition above, then `save_token xoxb- slack-bot-token`.
The app-level token is kept. Then restart the steward.

### Adding the file scopes to an app made before step 24.3

An app installed with only `chat:write`, `im:history` and `im:write` cannot fetch or send
files: Slack answers a download with a sign-in page (logged as `Slack answered a page`, the
file shown as not received) and an upload with `missing_scope`. Open the app's **App
Manifest** page, replace the manifest with `slack-app-manifest.json` (or add `files:read`
and `files:write` under **OAuth & Permissions → Bot Token Scopes**), **Save Changes**, then
**reinstall** the app (**Install App → Reinstall to Workspace → Allow**): new scopes take
effect only on a reinstall. If the Bot User OAuth Token shown afterwards differs from the
saved one, save it again (`save_token xoxb- slack-bot-token`, as above) and restart the
steward. Files sent before the reinstall are not fetched again.

### Adding the group scopes to an app made before step 23 (Slack private channels)

An app installed before step 23 has no `app_mention` event and no `app_mentions:read` or
`groups:read` scope: a mention in a channel never reaches the steward. Open the app's **App
Manifest** page, replace the whole manifest with `slack-app-manifest.json` (JSON tab),
**Save Changes**, then **reinstall** the app (**Install App → Reinstall to Workspace →
Allow**): new scopes and events take effect only on a reinstall. If the Bot User OAuth Token
shown afterwards differs from the saved one, save it again (`save_token xoxb-
slack-bot-token`, as above) and restart the steward.

Then bind a channel, on the dashboard and in Slack:

1. Make (or pick) a **private** channel that is not a Slack Connect channel (no other
   organization in it). Public channels, shared channels and group DMs cannot be bound.
2. Invite the steward: `/invite @AItelier Steward` in that channel.
3. Dashboard → steward → **Channels** → Slack → **Add group**: give the group a name (unlike
   your other groups') and take the code (`G-XXXX-XXXX`, 10 minutes, once).
4. In the channel, **from your bound Slack account**, send `@AItelier Steward G-XXXX-XXXX`
   and nothing else. The steward answers with its one-line notice; a refused code is
   answered with nothing (the steward's log says why).
5. Anyone in the channel can now ask it by mentioning it; it answers in the asker's thread.
   A follow-up inside the thread must mention it again (only mentions reach the app).

### Where the tokens live, and the restart

- The tokens are `$HOME/.aitelier-secrets/slack-app-token` and
  `$HOME/.aitelier-secrets/slack-bot-token`, one token per file, mode 0600. They are read,
  never logged. The directory is HOME-relative and fixed: no env var moves it
  (`AITELIER_SECRETS_DIR` does not), for this plugin or for the host that decides to load
  it. With either missing the channel registers as disconnected and does nothing.
- The steward's profile loads this plugin only when both files exist, and that is checked
  when the profile is written, at the steward's start: tokens added to a running steward
  load nothing. Until the restart the dashboard's Channels tab shows no status for Slack
  at all (or, if it ran before, its last report); after it, `connected` or why not.

The Slack SDK (`@slack/socket-mode` 3.1.1, `@slack/web-api` 8.3.0, pinned exactly) is a
dependency of the DSH runtime install (`integrations/dsh-driver/package.json`), and is
resolved from the running DSH entry, not from this directory. `@slack/socket-mode` also
needs `undici`, which it declares as a peer dependency (`^7 || ^8`); npm 7+ installs peer
dependencies itself, so it needs no pin of its own (the lock has 8.11.2).

## Accepted risks

These hold for every owner channel (Slack and WeChat):

- **A message can be delivered to the owner twice after a long record outage.** The
  steward's message is sent first and its delivery row recorded after (the row is what
  keeps a restart from sending it again: `sourceOf`). The record is retried 6 times,
  1+2+4+8+16 s - about 31 s - and then logged as failed. If the record endpoint cannot be
  reached for longer than that (a redeploy that includes container start-up, a 502 window),
  the message went out with no row, so a steward start that replays the same State message
  id sends the owner a second copy. Consequence: a duplicate, never a silent loss; the
  duplicate is one message and the owner can see it is the same text. Revisit when the
  delivery can be recorded durably before the send (a reservation the send confirms), or
  when a channel offers an idempotent send keyed by message id.
- **A decision's line is sent at least once, not exactly once.** It is owed while its
  answer has not come back or its send failed, and the owed store is cleared only after
  the send, so the two windows that stay are a duplicate line, never a silent one: with no
  State dir (the owe in memory only) a restart loses the owe, and a stop between a
  successful send and the store's clear re-sends that one line after the restart. Revisit
  when the host can be told a line went out (an acknowledgement command for answers),
  which would make it exactly-once.
- Approval power over chat rests on the steward host's driver token. A process running
  as the same user that holds it could relay an approval phrase in-protocol with false
  attribution (recorded as decided by the owner on a channel). Under the current
  same-user trust model this is no new boundary: such a process could already write
  `aitelier.db`. A later hardening is a separate, host-only credential for the channel
  commands.
- A stranger who can DM the bot can retire a live binding code with 5 wrong guesses. It
  cannot take the binding over: the code is stored only as an HMAC keyed with the driver
  pepper secret file (like the driver tokens), so a copy of the State database does not
  reveal it - without the pepper no code is issued or checked (`channel_secret_missing`) -
  and online guessing stays bounded at 5 guesses in 10^6. The State writers never persist
  a live code, wherever it sits in an owner-written text (relayed, or typed on the
  dashboard, body or subject): the write is refused `code_sent_to_bound_account` and the
  code is retired, so the owner issues a new one. The refusals are in the log with the
  sender, and the owner just issues a new code.
- One place beside the State database holds owner text: the director's owed store
  (`channel-owed.json` in the director's own state dir, mode 0600, `createOwnerChannels` in
  `integrations/dsh-director/lib/channels.mjs`). It records that a message is owed BEFORE
  any host call, so a stop during the call loses nothing, and it holds the message's text
  only after the host has allowed it: `channels.codeCheck` answers with the rule the relay
  itself applies (`core/state_channels.py:_store_text_verdict` - a bound sender, and no
  live binding code anywhere in the text in any rendering the backend folds), so the text
  that reaches the file cannot be the live code, and a text that could be is never written
  there at all. The plugin keeps no digit rule of its own: measured on this tree, six
  digits in a row silenced an approval whose proposal id carried one (about 38% of real
  ids), and `\p{Nd}{6}` missed the 99 code points that fold to a digit (superscripts,
  circled, dingbat), leaving the live code in the file. It is 0600 in the director's own
  directory (not the world-readable State database) and is cleared as soon as the host
  answers. A transient refusal cannot put a code there either: the backend answers a
  code-carrying body before it answers `rate_limited`. Revisit if a channel client ever
  holds text for longer than a code's life, or if the owed file moves out of the director's
  own directory.
- Token files (`slack-app-token`, `slack-bot-token`, `wechat-bridge-token`) should be
  mode 0600; nothing enforces it.

## Tests

`node --test --test-concurrency=1 test/slack.test.mjs` - fake socket and web clients, no
network. Two tests load the real SDK from `integrations/dsh-driver/node_modules`, so
run `npm ci` there first. The last test pins `slack-app-manifest.json` to the plugin:
Socket Mode on, no Request URL and no token in it, bot scopes exactly those of every Web
API method `index.mjs` calls (`filesUploadV2` as `files:write`) plus `files:read` for the
file download and `im:history` for its one event `message.im`, and no Agents & AI Apps
feature. The file tests use a fake fetch and a fake `storeMedia`: no Slack call.
