# dsh-channel-slack

The owner's Slack direct messages as an owner channel: a DSH plugin that registers the
channel `slack` with the `ownerChannels` service (provided by dsh-director when its host
declares the `channels` capability). Design: step 19, §7.2.

## What it does

- A person's direct message to the bot (`message` event, `channel_type: im`, no subtype,
  not from a bot) → `ownerChannels.inbound({channel: 'slack', senderRef: <user id>,
  messageRef: <client_msg_id or ts>, text, at})`. Every Socket Mode envelope is
  acknowledged, whatever it is. Whether the sender is the bound owner is the service's
  decision, not this plugin's.
- `send(text, {replyToRef, to})` → posts to the DM of `to`, the account the service has
  bound (it re-reads the binding and passes it on every send). The plugin keeps no idea
  of its own about who the owner is; without `to` the send fails. Text over 3500
  characters is sent as consecutive messages; the first message's `ts` is the ref.
- `status()` → `{connected}` from the socket's own state events, with a `detail` when not
  connected (`connecting`, `disconnected`, `disconnected: <slack error>` such as
  `invalid_auth`, or why it is not configured).
- Reconnects itself: the SDK's own reconnect is turned off (`autoReconnectEnabled: false`)
  because in @slack/socket-mode 3.1.1 a reconnect that Slack refuses (a revoked token,
  for instance) leaves a rejected promise nobody handles, which would end the steward's
  process. The plugin retries every failed or dropped connection itself, 1 s doubling to
  at most 5 min, and stops on dispose.

## Setup

1. Create a Slack app, enable **Socket Mode**, and create an app-level token with
   `connections:write` (starts `xapp-`).
2. Bot token scopes: `im:history`, `chat:write`, `im:write`; subscribe to the bot event
   `message.im`; in App Home allow users to send messages to the app. Install it to the
   workspace (bot token starts `xoxb-`).
3. Put the tokens, one per file, in `~/.aitelier-secrets/slack-app-token` and
   `~/.aitelier-secrets/slack-bot-token` (mode 0600). They are read, never logged. With
   either missing the channel registers as disconnected and does nothing.
4. Run one instance per app: Slack spreads Socket Mode events over all of an app's open
   connections, so a second process would receive part of the owner's messages.

The Slack SDK (`@slack/socket-mode` 3.1.1, `@slack/web-api` 8.3.0, pinned exactly) is a
dependency of the DSH runtime install (`integrations/dsh-driver/package.json`), and is
resolved from the running DSH entry, not from this directory. `@slack/socket-mode` also
needs `undici`, which it declares as a peer dependency (`^7 || ^8`); npm 7+ installs peer
dependencies itself, so it needs no pin of its own (the lock has 8.11.2).

## Accepted risks

These hold for every owner channel (Slack and WeChat):

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
  and online guessing stays bounded at 5 guesses in 10^6. The refusals are in the log
  with the sender, and the owner just issues a new code.
- Token files (`slack-app-token`, `slack-bot-token`, `wechat-bridge-token`) should be
  mode 0600; nothing enforces it.

## Tests

`node --test --test-concurrency=1 test/slack.test.mjs` - fake socket and web clients, no
network. The last test loads the real SDK from `integrations/dsh-driver/node_modules`, so
run `npm ci` there first.
