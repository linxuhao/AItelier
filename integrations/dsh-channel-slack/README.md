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
  sent is owed, not lost. Without a State dir the owe lives in memory only, so a restart
  loses it.
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
3. Put the tokens, one per file, in `$HOME/.aitelier-secrets/slack-app-token` and
   `$HOME/.aitelier-secrets/slack-bot-token` (mode 0600). They are read, never logged.
   The directory is HOME-relative and fixed: no env var moves it
   (`AITELIER_SECRETS_DIR` does not), for this plugin or for the host that decides to
   load it. With either missing the channel registers as disconnected and does nothing.
4. Restart the steward. The steward's profile loads this plugin only when both files
   exist, and that is checked when the profile is written, at the steward's start:
   tokens added to a running steward load nothing. Until the restart the dashboard's
   Channels tab shows no status for Slack at all (or, if it ran before, its last report);
   after it, `connected` or why not.
5. Run one instance per app: Slack spreads Socket Mode events over all of an app's open
   connections, so a second process would receive part of the owner's messages.

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
  and online guessing stays bounded at 5 guesses in 10^6. A live code is never persisted
  either, wherever it sits in an owner-written message (relayed, or typed on the
  dashboard): the write is refused `code_sent_to_bound_account` and the code is retired,
  so the owner issues a new one. The refusals are in the log with the sender, and the
  owner just issues a new code.
- Token files (`slack-app-token`, `slack-bot-token`, `wechat-bridge-token`) should be
  mode 0600; nothing enforces it.

## Tests

`node --test --test-concurrency=1 test/slack.test.mjs` - fake socket and web clients, no
network. The last test loads the real SDK from `integrations/dsh-driver/node_modules`, so
run `npm ci` there first.
