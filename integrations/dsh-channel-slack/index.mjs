/** dsh-channel-slack: the owner's Slack direct messages as an owner channel (design 7.2).
 *
 *  A DSH plugin that registers the channel 'slack' with the `ownerChannels` service.
 *  It only moves text: a direct message to the bot goes to ownerChannels.inbound, and
 *  send(text) posts to a DM. Who is bound, what a message means and what may be sent
 *  are decided by the service and its host, never here - so this plugin keeps no
 *  binding and makes no judgement beyond "is this a person's DM to the bot".
 *
 *  Socket Mode (an outbound websocket) because the host has no public endpoint. The
 *  two tokens are read from files and never logged; with either missing the channel
 *  registers as disconnected and does nothing else. One process per token pair: Slack
 *  spreads a Socket Mode app's events over all its open connections, so a second
 *  instance would silently take half the owner's messages. */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const name = 'dsh-channel-slack';
export const inject = ['ownerChannels'];

// Slack truncates a message's text past 40000 characters and renders long ones badly
// well before that; 3500 keeps every part readable as one message (contract C).
export const SLACK_TEXT_LIMIT = 3500;
// Reconnect backoff: 1 s, doubling, at most 5 min between attempts. The SDK's own
// reconnect is off (it leaves a rejected promise nobody handles when Slack refuses the
// token, which would end the steward's process); this plugin reconnects instead.
export const reconnectDelay = attempt => Math.min(5 * 60_000, 1000 * 2 ** attempt);
// The answers that are TRANSIENT, not final: the host or the backend could not be
// reached (`unavailable`), the channel's minute's share is spent (`rate_limited` - a
// burst after an outage is ordinary), or the decision this message carries is still
// running (`approval_pending`). The message is held and asked again, never dropped; a
// refusal that names anything else (a body too long, an unbound sender) is final.
const RETRY_REASONS = new Set(['unavailable', 'rate_limited', 'approval_pending']);
// Messages one sender may have waiting while the host is unavailable.
export const HELD_PER_SENDER = 100;

// A pause that ends early when the channel stops; it never keeps the process alive.
const sleep = (ms, signal) => new Promise(resolve => {
  if (signal.aborted) return resolve();
  const timer = setTimeout(done, ms);
  timer.unref?.();
  function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); }
  signal.addEventListener('abort', done, { once: true });
});

/** The two tokens, or the reason there are none. Only the reason is ever printed. */
export function readTokens(dir) {
  const read = file => { try { return readFileSync(join(dir, file), 'utf8').trim(); } catch { return ''; } };
  const appToken = read('slack-app-token'), botToken = read('slack-bot-token');
  if (!appToken || !botToken) return { reason: 'not configured: slack-app-token / slack-bot-token missing' };
  if (!appToken.startsWith('xapp-')) return { reason: 'slack-app-token is not an app-level (xapp-) token' };
  if (!botToken.startsWith('xoxb-')) return { reason: 'slack-bot-token is not a bot (xoxb-) token' };
  return { appToken, botToken };
}

/** A person's direct message to the bot - not an edit, a join, a bot or another channel. */
export function isOwnerDm(event) {
  return event?.type === 'message' && event.channel_type === 'im' && !event.subtype && !event.bot_id
    && typeof event.user === 'string' && event.user !== '' && typeof event.text === 'string';
}

// Slack escapes exactly these three in message text, both ways
// (docs.slack.dev/messaging/formatting-message-text#escaping): unescaped on the way in
// so the owner's words reach the steward as typed, escaped on the way out so a `<` in
// the steward's text is shown, not read as a link or mention.
export const unescapeSlack = text => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
export const escapeSlack = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Consecutive parts of at most `limit` characters (code points, so no emoji is cut in
 *  half), each ending at a line break when one falls in its second half. */
export function splitText(text, limit = SLACK_TEXT_LIMIT) {
  const chars = Array.from(text);
  const parts = [];
  while (chars.length > limit) {
    const newline = chars.lastIndexOf('\n', limit - 1);
    const cut = newline >= limit / 2 ? newline + 1 : limit;
    parts.push(chars.splice(0, cut).join(''));
  }
  parts.push(chars.join(''));
  return parts;
}

/** The channel itself, with the Slack clients passed in (fakes in tests). */
export function slackChannel({ ownerChannels, appToken, botToken, SocketModeClient, WebClient, logLevel, log, unavailable,
  backoff = reconnectDelay, retryMs = 5000, maxRetryMs = 60_000 }) {
  const configured = !unavailable && appToken && botToken && SocketModeClient && WebClient;
  let state = { connected: false, detail: configured ? 'connecting' : (unavailable ?? 'not configured') };
  let socket, web, stopped = false;
  let connecting, retryTimer, failures = 0;
  const dms = new Map(); // user -> DM channel id
  const stopping = new AbortController();
  const lanes = new Map(); // user -> { run: the delivery of their latest message, held: messages waiting }

  /** Passes one message on. An acknowledged envelope is never redelivered by Slack, so
   *  this is the message's only copy: while the host answers a transient reason
   *  (`unavailable`, `rate_limited`, `approval_pending`) - or throws - it is held and
   *  retried with a growing pause, under the same message ref, which the host takes
   *  once. It ends when the host has answered for good (accepted or refused) or
   *  the channel stops. */
  async function deliver(message) {
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      let reason;
      try {
        const result = await ownerChannels.inbound(message);
        if (result?.accepted || !RETRY_REASONS.has(result?.reason)) return;
        reason = result.reason;
      } catch (error) {
        reason = error?.message ?? error;
      }
      // Never the text: what a stranger sent is not logged (design 7.2 B3).
      log.warn(`slack: inbound from ${message.senderRef} (${message.text.length} chars) not delivered (attempt ${attempt}): `
        + `${String(reason).slice(0, 200)}; retrying`);
      await sleep(pause, stopping.signal);
      pause = Math.min(pause * 2, maxRetryMs);
    }
  }

  // Each sender's messages go on in the order they arrived: a held one is not overtaken
  // by the next. Other senders are not held up by it. A sender with HELD_PER_SENDER
  // messages already waiting (the host down, a stranger flooding) gets no more held.
  function enqueue(message) {
    const lane = lanes.get(message.senderRef) ?? { run: Promise.resolve(), held: 0 };
    if (lane.held >= HELD_PER_SENDER) {
      log.warn(`slack: inbound from ${message.senderRef} (${message.text.length} chars) not held: `
        + `${HELD_PER_SENDER} messages from this sender are already waiting`);
      return lane.run;
    }
    lane.held++;
    lane.run = lane.run.then(() => deliver(message)).finally(() => {
      if (--lane.held === 0 && lanes.get(message.senderRef) === lane) lanes.delete(message.senderRef);
    });
    lanes.set(message.senderRef, lane);
    return lane.run;
  }

  async function onEnvelope({ ack, type, body }) {
    // Every envelope is acknowledged, first: Slack redelivers an unacknowledged one after
    // 3 s, and handing a message to the service can take longer than that.
    try { await ack(); } catch (error) { log.warn(`slack: ack failed: ${error?.message ?? error}`); }
    const event = type === 'events_api' ? body?.event : undefined;
    if (!isOwnerDm(event)) return;
    const messageRef = event.client_msg_id || event.ts;
    const at = Number.isFinite(Number(event.ts)) ? new Date(Number(event.ts) * 1000).toISOString() : undefined;
    return enqueue({ channel: 'slack', senderRef: event.user, messageRef, text: unescapeSlack(event.text), at });
  }

  // One connection attempt at a time; every failure, now or after a drop, is caught here
  // and becomes a later attempt, so nothing the socket does can reject into the process.
  function connect() {
    if (stopped || connecting) return connecting;
    connecting = (async () => {
      try {
        await socket.start();
        failures = 0;
        // stop() ran while this attempt was in flight: close what it just opened.
        if (stopped) await socket.disconnect();
      } catch (error) {
        if (stopped) return;
        // Slack's error code (invalid_auth, ...) names the cause; the token is never in it.
        const cause = error?.data?.error ?? error?.code ?? error?.message ?? 'connection closed';
        state = { connected: false, detail: `disconnected: ${String(cause).slice(0, 200)}` };
        log.warn(`slack: ${state.detail}`);
        retry();
      } finally {
        connecting = undefined;
      }
    })();
    return connecting;
  }

  function retry() {
    if (stopped || retryTimer) return;
    retryTimer = setTimeout(() => { retryTimer = undefined; void connect(); }, backoff(failures++));
    retryTimer.unref?.();
  }

  async function start() {
    if (!configured) return;
    web = new WebClient(botToken, { logLevel });
    socket = new SocketModeClient({ appToken, logLevel, autoReconnectEnabled: false });
    socket.on('connected', () => { if (!stopped) state = { connected: true }; });
    socket.on('disconnected', () => {
      // A failed attempt is handled where it was made; a drop of a live socket starts a new one.
      if (connecting && !stopped) return;
      state = { connected: false, detail: 'disconnected' };
      retry();
    });
    socket.on('slack_event', envelope => { void onEnvelope(envelope); });
    await connect();
  }

  // `to` is the bound account, given by the service on every send (it re-reads the binding
  // each time); this plugin keeps no idea of its own about who the owner is.
  async function send(text, { to } = {}) {
    if (!configured) throw new Error(`slack channel ${state.detail}`);
    if (typeof to !== 'string' || to === '') throw new Error('slack: send needs the account to send to (options.to)');
    if (!dms.has(to)) {
      const opened = await web.conversations.open({ users: to });
      dms.set(to, opened.channel.id);
    }
    const refs = [];
    for (const part of splitText(text)) {
      const posted = await web.chat.postMessage({ channel: dms.get(to), text: escapeSlack(part) });
      refs.push(posted.ts);
    }
    return { ref: refs[0] };
  }

  const status = () => ({ ...state });
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    stopping.abort();
    clearTimeout(retryTimer);
    retryTimer = undefined;
    state = { connected: false, detail: 'disconnected' };
    await socket?.disconnect?.().catch(error => log.warn(`slack: disconnect failed: ${error?.message ?? error}`));
  };
  return { start, stop, send, status, onEnvelope };
}

/** The pinned Slack SDK (contract C: @slack/socket-mode 3.1.1, @slack/web-api 8.3.0). It
 *  is installed with the DSH runtime (integrations/dsh-driver/package.json), not beside
 *  this file, so it is resolved from the running DSH entry, as dsh-director resolves the
 *  DSH packages. */
export function loadSlack(resolveFrom = process.argv[1]) {
  const require = createRequire(resolveFrom);
  const { SocketModeClient, LogLevel } = require('@slack/socket-mode');
  const { WebClient } = require('@slack/web-api');
  return { SocketModeClient, WebClient, logLevel: LogLevel.WARN };
}

export function apply(ctx, config = {}) {
  const log = ctx.logger ?? console;
  const tokens = readTokens(config.secretsDir ?? join(homedir(), '.aitelier-secrets'));
  let slack = {}, unavailable = tokens.reason;
  if (!unavailable) {
    try { slack = loadSlack(config.resolveFrom); } catch (error) { unavailable = `slack SDK not installed: ${error?.code ?? error?.message}`; }
  }
  if (unavailable) log.info(`slack channel: ${unavailable}`);
  const channel = slackChannel({ ownerChannels: ctx.ownerChannels, ...tokens, ...slack, log, unavailable });
  const unregister = ctx.ownerChannels.register({ name: 'slack', send: channel.send, status: channel.status });
  void channel.start();
  ctx.on('dispose', () => { unregister?.(); return channel.stop(); });
}
