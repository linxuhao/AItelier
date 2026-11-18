/** dsh-channel-slack against a fake socket client and a fake web client: no network, no
 *  tokens. The last test loads the real pinned SDK only to check the names this plugin
 *  relies on (constructors, the LogLevel, the socket's state events). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { apply, inject, isOwnerDm, loadSlack, name, readTokens, reconnectDelay, slackChannel, splitText, SLACK_TEXT_LIMIT } from '../index.mjs';

const quiet = { info() {}, warn() {} };

class FakeSocket extends EventEmitter {
  static last;
  constructor(options) { super(); this.options = options; this.disconnected = false; FakeSocket.last = this; }
  async start() { this.emit('connected'); }
  async disconnect() { this.disconnected = true; this.emit('disconnected'); }
}

class FakeWeb {
  static last;
  constructor(token, options) {
    this.token = token; this.options = options; this.posted = []; this.opened = []; FakeWeb.last = this;
    this.conversations = { open: async ({ users }) => (this.opened.push(users), { ok: true, channel: { id: 'D-' + users } }) };
    this.chat = { postMessage: async args => (this.posted.push(args), { ok: true, ts: `17000000${this.posted.length}.000100` }) };
  }
}

const registry = (answer = () => ({ accepted: true })) => {
  const calls = [];
  return { calls, inbound: async message => (calls.push(message), answer(message)) };
};

const started = async (ownerChannels = registry(), extra = {}) => {
  const channel = slackChannel({ ownerChannels, appToken: 'xapp-1', botToken: 'xoxb-1',
    SocketModeClient: FakeSocket, WebClient: FakeWeb, log: quiet, ...extra });
  await channel.start();
  return { channel, ownerChannels, socket: FakeSocket.last, web: FakeWeb.last };
};

// One Socket Mode envelope as SocketModeClient emits it on 'slack_event'.
const envelope = (event, type = 'events_api') => {
  const acks = [];
  return { acks, ack: async () => { acks.push(true); }, envelope_id: 'e-1', type, body: { event } };
};
const dm = (over = {}) => ({ type: 'message', channel_type: 'im', channel: 'D-U1', user: 'U1', text: 'hello',
  ts: '1763481600.000200', client_msg_id: 'c-1', ...over });
const deliver = async (socket, env) => { socket.emit('slack_event', env); await new Promise(r => setImmediate(r)); };
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

test('a direct message to the bot reaches ownerChannels.inbound with the channel, sender, ref, text and time', async () => {
  const { socket, ownerChannels } = await started();
  const env = envelope(dm({ text: '同意 proposal p-1' }));
  await deliver(socket, env);
  assert.deepEqual(ownerChannels.calls, [{ channel: 'slack', senderRef: 'U1', messageRef: 'c-1',
    text: '同意 proposal p-1', at: '2025-11-18T16:00:00.000Z' }]);
  assert.deepEqual(env.acks, [true]);
});

test('without a client_msg_id the message ts is the message ref', async () => {
  const { socket, ownerChannels } = await started();
  await deliver(socket, envelope(dm({ client_msg_id: undefined })));
  assert.equal(ownerChannels.calls[0].messageRef, '1763481600.000200');
});

test("Slack's three escapes are undone on the way in and applied on the way out", async () => {
  const { channel, socket, ownerChannels, web } = await started();
  await deliver(socket, envelope(dm({ text: 'a &lt;b&gt; &amp;lt; c' })));
  assert.equal(ownerChannels.calls[0].text, 'a <b> &lt; c');
  await channel.send('x < y & z', { replyToRef: 'c-1', to: 'U1' });
  assert.equal(web.posted[0].text, 'x &lt; y &amp; z');
});

test('bot messages, subtypes, other channel types and other event types are ignored, and still acked', async () => {
  const { socket, ownerChannels } = await started();
  const ignored = [
    dm({ bot_id: 'B1' }),
    dm({ subtype: 'message_changed' }),
    dm({ subtype: 'bot_message' }),
    dm({ channel_type: 'channel' }),
    dm({ channel_type: 'mpim' }),
    dm({ user: undefined }),
    dm({ text: undefined }),
    { type: 'app_mention', user: 'U1', text: 'hi', channel_type: 'im' },
  ];
  const envs = ignored.map(event => envelope(event));
  envs.push(envelope(undefined, 'interactive'), envelope(undefined, 'slash_commands'));
  for (const env of envs) await deliver(socket, env);
  assert.deepEqual(ownerChannels.calls, []);
  assert.deepEqual(envs.map(env => env.acks.length), envs.map(() => 1), 'every envelope is acknowledged exactly once');
  assert.equal(isOwnerDm(dm()), true);
});

test('the envelope is acked before the service is asked, so a slow answer does not make Slack redeliver', async () => {
  const calls = [];
  const ownerChannels = { inbound: message => (calls.push(message), new Promise(() => {})) }; // never answers
  const { socket } = await started(ownerChannels);
  const env = envelope(dm());
  await deliver(socket, env);
  assert.equal(calls.length, 1, 'the message reached the service');
  assert.deepEqual(env.acks, [true], 'and was acknowledged while the service is still working on it');
});

test('the envelope is acked even when the service throws, and the text is not logged', async () => {
  const warnings = [];
  const ownerChannels = { inbound: async () => { throw new Error('backend down'); } };
  const { socket } = await started(ownerChannels, { log: { info() {}, warn: line => warnings.push(line) } });
  const env = envelope(dm({ text: 'secret words' }));
  await deliver(socket, env);
  assert.deepEqual(env.acks, [true]);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /U1 \(12 chars\).*backend down/);
  assert.doesNotMatch(warnings[0], /secret/);
});

test('send posts to the DM of the account the service names, whatever wrote what before', async () => {
  const { channel, socket, web } = await started();
  // The service passes the bound account as `to` on every send; refs this process has
  // never seen (written before a restart) and other senders change nothing.
  await deliver(socket, envelope(dm({ user: 'U1', client_msg_id: 'c-1' })));
  await deliver(socket, envelope(dm({ user: 'U2', client_msg_id: 'c-2', text: '123456' })));
  assert.deepEqual(await channel.send('bound', { replyToRef: 'c-2', to: 'U2' }), { ref: '170000001.000100' });
  assert.deepEqual(await channel.send('a reply to a message from before the restart', { replyToRef: 'old-ref', to: 'U2' }),
    { ref: '170000002.000100' });
  await channel.send('unprompted', { to: 'U2' });
  await channel.send('to the writer of c-1? no: to whom the service says', { replyToRef: 'c-1', to: 'U2' });
  assert.deepEqual(web.posted.map(p => p.channel), ['D-U2', 'D-U2', 'D-U2', 'D-U2']);
  assert.deepEqual(web.opened, ['U2'], 'the DM channel is opened once and reused');
});

test('send without an account to send to fails and posts nothing, even after messages came in', async () => {
  const { channel, socket, web } = await started();
  await deliver(socket, envelope(dm()));
  await assert.rejects(channel.send('hi'), /options\.to/);
  await assert.rejects(channel.send('hi', { replyToRef: 'c-1' }), /options\.to/);
  assert.deepEqual(web.posted, []);
  assert.deepEqual(web.opened, []);
});

test('send splits text longer than 3500 characters into consecutive messages and returns the first ref', async () => {
  const { channel, socket, web } = await started();
  await deliver(socket, envelope(dm()));
  const text = 'a'.repeat(SLACK_TEXT_LIMIT) + 'b'.repeat(SLACK_TEXT_LIMIT) + 'c';
  assert.deepEqual(await channel.send(text, { replyToRef: 'c-1', to: 'U1' }), { ref: '170000001.000100' });
  assert.deepEqual(web.posted.map(p => p.text), ['a'.repeat(SLACK_TEXT_LIMIT), 'b'.repeat(SLACK_TEXT_LIMIT), 'c']);
  await channel.send('a'.repeat(SLACK_TEXT_LIMIT), { to: 'U1' });
  assert.equal(web.posted.length, 4, 'exactly 3500 characters is one message');
});

test('splitText: parts end at a line break in their second half, and no code point is cut', () => {
  assert.deepEqual(splitText('ab\ncdef', 4), ['ab\n', 'cdef']);
  assert.deepEqual(splitText('a\nbcdefgh', 4), ['a\nbc', 'defg', 'h'], 'a break in the first half is not used');
  assert.deepEqual(splitText('😀😀😀', 2), ['😀😀', '😀']);
  assert.deepEqual(splitText(''), ['']);
  const long = Array.from({ length: 200 }, (_, i) => `line ${i} `.padEnd(40, '.')).join('\n');
  const parts = splitText(long);
  assert.equal(parts.join(''), long);
  assert.ok(parts.every(part => Array.from(part).length <= SLACK_TEXT_LIMIT));
});

test('the socket is made without the SDK reconnect; status follows it: connecting, connected, disconnected', async () => {
  const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-1', botToken: 'xoxb-1',
    SocketModeClient: FakeSocket, WebClient: FakeWeb, log: quiet, backoff: () => 60_000 });
  assert.deepEqual(channel.status(), { connected: false, detail: 'connecting' });
  await channel.start();
  const socket = FakeSocket.last;
  assert.equal(socket.options.autoReconnectEnabled, false);
  assert.deepEqual(channel.status(), { connected: true });
  socket.emit('disconnected'); // dropped: shown at once, not after the SDK's reconnect delay
  assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected' });
  socket.emit('connected');
  await channel.stop();
  assert.equal(socket.disconnected, true);
  assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected' });
});

// A socket shaped like the SDK's with its reconnect off: start() resolves on 'connected'
// and rejects when it cannot connect; a drop emits 'disconnected'. Each start() takes the
// next outcome from `outcomes` ('ok', an Error, or a gate promise to wait on first).
const scripted = outcomes => class extends FakeSocket {
  constructor(options) { super(options); this.starts = 0; this.open = false; }
  async start() {
    this.starts += 1;
    let next = outcomes.shift() ?? 'ok';
    if (next instanceof Promise) next = await next;
    if (next instanceof Error) throw next;
    this.open = true;
    this.emit('connected');
  }
  async disconnect() { this.disconnected = true; this.open = false; this.emit('disconnected'); }
};
const invalidAuth = () => Object.assign(new Error('An API error occurred: invalid_auth'), { data: { error: 'invalid_auth' } });

test('a drop is reconnected by the plugin; a failing reconnect is caught, shown with its cause, and retried', async () => {
  const rejections = [];
  const onRejection = reason => rejections.push(reason);
  process.on('unhandledRejection', onRejection);
  try {
    const warnings = [], delays = [];
    const Socket = scripted(['ok', invalidAuth(), invalidAuth(), 'ok']);
    const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-SECRET', botToken: 'xoxb-SECRET',
      SocketModeClient: Socket, WebClient: FakeWeb, log: { info() {}, warn: line => warnings.push(line) },
      backoff: attempt => (delays.push(attempt), 0) });
    await channel.start();
    const socket = FakeSocket.last;
    assert.deepEqual(channel.status(), { connected: true });
    socket.emit('disconnected'); // Slack refreshed the connection
    await tick(); // first reconnect: invalid_auth (the token was revoked meanwhile)
    assert.equal(socket.starts, 2);
    assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected: invalid_auth' });
    await tick(); await tick(); // second reconnect fails too, the third connects
    assert.equal(socket.starts, 4);
    assert.deepEqual(channel.status(), { connected: true });
    assert.deepEqual(delays, [0, 1, 2], 'the backoff grows with each failure in a row');
    socket.emit('disconnected');
    await tick();
    assert.equal(socket.starts, 5);
    assert.deepEqual(delays, [0, 1, 2, 0], 'a connection resets the backoff');
    await channel.stop();
    assert.doesNotMatch(warnings.join('\n'), /SECRET/);
    await tick(20);
    assert.deepEqual(rejections, []);
  } finally {
    process.off('unhandledRejection', onRejection);
  }
});

test('reconnectDelay: 1 s, doubling, at most 5 minutes', () => {
  assert.deepEqual([0, 1, 2, 3].map(reconnectDelay), [1000, 2000, 4000, 8000]);
  assert.equal(reconnectDelay(20), 300_000);
});

test('after stop nothing reconnects, and a start still in flight is closed when it connects', async () => {
  let open;
  const gate = new Promise(resolve => { open = resolve; });
  const Socket = scripted([gate]);
  const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-1', botToken: 'xoxb-1',
    SocketModeClient: Socket, WebClient: FakeWeb, log: quiet, backoff: () => 0 });
  const starting = channel.start(); // apply() does not await this
  const socket = FakeSocket.last;
  await channel.stop(); // disposed while Slack is still being asked for a URL
  open('ok');
  await starting;
  await tick(5);
  assert.equal(socket.open, false, 'the connection made after stop was closed');
  assert.equal(socket.starts, 1, 'and no reconnect followed');
  assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected' });
});

test('a socket that cannot start reports why, without the token, and does not crash', async () => {
  const warnings = [];
  const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-SECRET', botToken: 'xoxb-SECRET',
    SocketModeClient: scripted([invalidAuth()]), WebClient: FakeWeb, backoff: () => 60_000,
    log: { info() {}, warn: line => warnings.push(line) } });
  await channel.start();
  assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected: invalid_auth' });
  assert.doesNotMatch(warnings.join('\n'), /SECRET/);
  await channel.stop();
});

test('with the real SDK, a dropped socket whose reconnect Slack refuses does not reject into the process', async () => {
  // The reviewer's reproduction: with the SDK's own reconnect on, a 'close' schedules
  // start() with no catch, and invalid_auth from apps.connections.open escapes as an
  // unhandled rejection - which ends the steward's process.
  const driver = fileURLToPath(new URL('../../dsh-driver/package.json', import.meta.url));
  const slack = loadSlack(driver);
  const { WebAPIPlatformError } = createRequire(driver)('@slack/web-api');
  let opens = 0;
  class Refused extends slack.SocketModeClient {
    constructor(options) {
      super({ ...options, clientPingTimeout: 5 }); // the SDK's reconnect delay, made short
      this.webClient.apps.connections.open = async () => { opens += 1; throw new WebAPIPlatformError({ ok: false, error: 'invalid_auth' }); };
      Refused.last = this;
    }
  }
  const rejections = [];
  const onRejection = reason => rejections.push(reason);
  process.on('unhandledRejection', onRejection);
  try {
    const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-test', botToken: 'xoxb-test',
      SocketModeClient: Refused, WebClient: slack.WebClient, logLevel: 'error', log: quiet, backoff: () => 5 });
    await channel.start();
    assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected: invalid_auth' });
    Refused.last.emit('close'); // what the SDK's websocket emits when the connection drops
    await tick(60);
    await channel.stop();
    const after = opens;
    await tick(30);
    assert.deepEqual(rejections.map(r => r?.data?.error ?? r?.message), []);
    assert.ok(after >= 3, `the plugin kept retrying (${after} attempts)`);
    assert.equal(opens, after, 'and stopped after stop()');
  } finally {
    process.off('unhandledRejection', onRejection);
  }
});

test('readTokens: both files present and well-formed, or a reason that carries no token', () => {
  const dir = mkdtempSync(join(tmpdir(), 'slack-tokens-'));
  assert.match(readTokens(dir).reason, /missing/);
  writeFileSync(join(dir, 'slack-app-token'), 'xapp-1-AAA\n');
  assert.match(readTokens(dir).reason, /missing/);
  writeFileSync(join(dir, 'slack-bot-token'), 'xapp-WRONG\n');
  assert.equal(readTokens(dir).reason, 'slack-bot-token is not a bot (xoxb-) token');
  writeFileSync(join(dir, 'slack-bot-token'), ' xoxb-2-BBB \n');
  assert.deepEqual(readTokens(dir), { appToken: 'xapp-1-AAA', botToken: 'xoxb-2-BBB' });
  writeFileSync(join(dir, 'slack-app-token'), 'xoxb-WRONG');
  assert.equal(readTokens(dir).reason, 'slack-app-token is not an app-level (xapp-) token');
});

test('apply with no token files: registers slack as disconnected, opens no socket, sends nothing, does not crash', async () => {
  const registered = [], disposers = [];
  let unregistered = 0;
  FakeSocket.last = undefined;
  const ctx = { logger: quiet, on: (event, fn) => { assert.equal(event, 'dispose'); disposers.push(fn); },
    ownerChannels: { register: channel => (registered.push(channel), () => { unregistered += 1; }),
      inbound: async () => assert.fail('nothing may arrive') } };
  apply(ctx, { secretsDir: mkdtempSync(join(tmpdir(), 'slack-none-')) });
  assert.equal(name, 'dsh-channel-slack');
  assert.deepEqual(inject, ['ownerChannels']);
  assert.equal(registered.length, 1);
  assert.equal(registered[0].name, 'slack');
  assert.deepEqual(registered[0].status(), { connected: false, detail: 'not configured: slack-app-token / slack-bot-token missing' });
  await assert.rejects(registered[0].send('hi'), /not configured/);
  assert.equal(FakeSocket.last, undefined);
  await Promise.all(disposers.map(fn => fn()));
  assert.equal(unregistered, 1);
});

test('the pinned Slack SDK resolves from the DSH runtime install and has what this plugin uses', () => {
  // The runtime install mirrors integrations/dsh-driver/package.json; in a checkout that
  // is integrations/dsh-driver/node_modules (npm ci there). A missing install is red.
  const driver = fileURLToPath(new URL('../../dsh-driver/package.json', import.meta.url));
  const pinned = JSON.parse(readFileSync(driver, 'utf8')).dependencies;
  assert.equal(pinned['@slack/socket-mode'], '3.1.1');
  assert.equal(pinned['@slack/web-api'], '8.3.0');
  const slack = loadSlack(driver);
  assert.equal(typeof slack.SocketModeClient, 'function');
  assert.equal(typeof slack.WebClient, 'function');
  assert.equal(slack.logLevel, 'warn');
  const socket = new slack.SocketModeClient({ appToken: 'xapp-test', logLevel: slack.logLevel });
  // The state events status() listens to are the SDK's own names.
  const source = readFileSync(join(driver, '..', 'node_modules', '@slack', 'socket-mode', 'dist', 'src', 'SocketModeClient.js'), 'utf8');
  for (const state of ['connected', 'disconnected']) assert.match(source, new RegExp(`= "${state}"`));
  // The option that turns the SDK's own reconnect off, by the name this plugin passes.
  assert.match(source, /autoReconnectEnabled = true,/);
  assert.match(source, /this\.emit\('slack_event', \{\s*ack,/);
  assert.equal(typeof socket.start, 'function');
  assert.equal(typeof socket.disconnect, 'function');
  const web = new slack.WebClient('xoxb-test', { logLevel: slack.logLevel });
  assert.equal(typeof web.conversations.open, 'function');
  assert.equal(typeof web.chat.postMessage, 'function');
});
