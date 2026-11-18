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
- `send(text, {replyToRef})` → posts to the DM of whoever wrote `replyToRef`, or, with no
  ref, of the last account the service accepted a message from (kept in memory, so after a
  restart an unprompted message fails until the owner writes once). Text over 3500
  characters is sent as consecutive messages; the first message's `ts` is the ref.
- `status()` → `{connected}` from the socket's own state events, with a `detail` when not
  connected (`connecting`, `reconnecting`, `disconnected`, `start failed: <slack error>`,
  or why it is not configured).

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
resolved from the running DSH entry, not from this directory.

## Tests

`node --test --test-concurrency=1 test/slack.test.mjs` - fake socket and web clients, no
network. The last test loads the real SDK from `integrations/dsh-driver/node_modules`, so
run `npm ci` there first.
