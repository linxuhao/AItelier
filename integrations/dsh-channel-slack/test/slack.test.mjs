/** dsh-channel-slack against a fake socket client and a fake web client: no network, no
 *  tokens. The last test loads the real pinned SDK only to check the names this plugin
 *  relies on (constructors, the LogLevel, the socket's state events). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { existsSync, mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { apply, groupRefusal, HELD_PER_SENDER, inject, isGroupMention, isOwnerDm, loadSlack, MEDIA_LIMITS, name, readTokens,
  reconnectDelay, slackChannel, slackFileUrl, splitText, SLACK_TEXT_LIMIT, withoutMention } from '../index.mjs';

const quiet = { info() {}, warn() {} };

class FakeSocket extends EventEmitter {
  static last;
  constructor(options) { super(); this.options = options; this.disconnected = false; FakeSocket.last = this; }
  async start() { this.emit('connected'); }
  async disconnect() { this.disconnected = true; this.emit('disconnected'); }
}

// Step 23 (design-steward 7.7.19): the bot's own user id and what conversations.info says of a
// private channel (groups:read).
const BOT = 'UBOT';
const PRIVATE_CHANNEL = Object.freeze({ is_channel: true, is_group: false, is_im: false, is_mpim: false, is_private: true,
  is_ext_shared: false, is_pending_ext_shared: false, is_shared: false });

class FakeWeb {
  static last;
  constructor(token, options) {
    this.token = token; this.options = options; this.posted = []; this.opened = []; FakeWeb.last = this;
    // Step 23: conversations.info answers what `infos` says of a conversation (a private channel
    // unless a test says otherwise; an Error is thrown as the SDK throws it).
    this.infos = {}; this.infoCalls = [];
    this.conversations = { open: async ({ users }) => (this.opened.push(users), { ok: true, channel: { id: 'D-' + users } }),
      info: async ({ channel }) => {
        this.infoCalls.push(channel);
        const info = this.infos[channel] ?? { id: channel, ...PRIVATE_CHANNEL };
        if (info instanceof Error) throw info;
        return { ok: true, channel: info };
      } };
    this.auth = { test: async () => ({ ok: true, user_id: BOT }) };
    this.chat = { postMessage: async args => (this.posted.push(args), { ok: true, ts: `17000000${this.posted.length}.000100` }) };
    this.uploaded = [];
  }
  // The SDK's answer shape: one files.completeUploadExternal response per completed job.
  async filesUploadV2(args) { this.uploaded.push(args); return { ok: true, files: [{ ok: true, files: [{ id: 'F-' + this.uploaded.length }] }] }; }
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
const until = async (check, ms = 3000) => {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error('timed out waiting');
    await tick(5);
  }
};

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
  const { channel, socket } = await started(ownerChannels, { log: { info() {}, warn: line => warnings.push(line) } });
  const env = envelope(dm({ text: 'secret words' }));
  await deliver(socket, env);
  assert.deepEqual(env.acks, [true]);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /U1 \(12 chars\).*backend down/);
  assert.doesNotMatch(warnings[0], /secret/);
  await channel.stop();
});

// What the real service (dsh-director channels.inbound) answers when the host or the
// backend cannot be reached: it never rejects.
const unavailableFor = count => registry(() => (count-- > 0 ? { accepted: false, reason: 'unavailable' } : { accepted: true }));

test('an inbound the host answers unavailable is held and retried, under the same ref, until it is taken', async () => {
  const warnings = [];
  const { channel, socket, ownerChannels } = await started(unavailableFor(2),
    { log: { info() {}, warn: line => warnings.push(line) }, retryMs: 5 });
  const env = envelope(dm({ text: '同意 proposal p-1' }));
  socket.emit('slack_event', env);
  await until(() => ownerChannels.calls.length === 3);
  await tick(30);
  assert.deepEqual(ownerChannels.calls.map(m => m.messageRef), ['c-1', 'c-1', 'c-1'], 'held, not dropped, and taken once');
  assert.deepEqual(ownerChannels.calls[2], ownerChannels.calls[0], 'every attempt is the same message');
  assert.deepEqual(env.acks, [true], 'acked once, first');
  assert.equal(warnings.length, 2);
  assert.ok(warnings.every(line => /not delivered \(attempt [12]\): unavailable; retrying/.test(line) && !line.includes('同意')),
    'the text is never logged');
  await channel.stop();
});

test('an inbound that throws is retried the same way; a refusal other than a transient one is final', async () => {
  let failures = 2;
  const refusals = { 'c-2': 'channel_sender_not_bound', 'c-3': 'invalid_request' };
  const ownerChannels = registry(message => {
    if (message.messageRef === 'c-1' && failures-- > 0) throw new Error('host down');
    return refusals[message.messageRef] ? { accepted: false, reason: refusals[message.messageRef] } : { accepted: true };
  });
  const { channel, socket } = await started(ownerChannels, { retryMs: 5 });
  for (const ref of ['c-1', 'c-2', 'c-3']) socket.emit('slack_event', envelope(dm({ client_msg_id: ref })));
  await until(() => ownerChannels.calls.length === 5);
  await tick(30);
  assert.deepEqual(ownerChannels.calls.map(m => m.messageRef), ['c-1', 'c-1', 'c-1', 'c-2', 'c-3']);
  await channel.stop();
});

test('a transient refusal (rate_limited, approval_pending) is held and retried, never dropped', async () => {
  // The backend refuses a channel's 7th message per minute with rate_limited - what a
  // replay after an outage runs into - and answers approval_pending while an earlier
  // delivery's decision still runs. Neither is final: the message is this process's only
  // copy, so it is held and asked again instead of being dropped.
  const reasons = ['rate_limited', 'approval_pending'];
  const ownerChannels = registry(() => (reasons.length ? { accepted: false, reason: reasons.shift() } : { accepted: true }));
  const { channel, socket } = await started(ownerChannels, { retryMs: 5 });
  const env = envelope(dm({ client_msg_id: 'r-1' }));
  socket.emit('slack_event', env);
  await until(() => ownerChannels.calls.length === 3);
  await tick(30);
  assert.deepEqual(ownerChannels.calls.map(m => m.messageRef), ['r-1', 'r-1', 'r-1'], 'held under the same ref');
  assert.deepEqual(ownerChannels.calls[2], ownerChannels.calls[0], 'every attempt is the same message');
  assert.deepEqual(env.acks, [true], 'acked once, first');
  await channel.stop();
  const seen = ownerChannels.calls.length;
  await tick(30);
  assert.equal(ownerChannels.calls.length, seen, 'and not asked again after stop');
});

test("a held message is not overtaken by its sender's next one; another sender is not held up by it", async () => {
  let held = 3;
  const ownerChannels = registry(message =>
    message.messageRef === 'c-1' && held-- > 0 ? { accepted: false, reason: 'unavailable' } : { accepted: true });
  const { channel, socket } = await started(ownerChannels, { retryMs: 10 });
  socket.emit('slack_event', envelope(dm({ client_msg_id: 'c-1' })));
  socket.emit('slack_event', envelope(dm({ client_msg_id: 'c-2' })));
  socket.emit('slack_event', envelope(dm({ client_msg_id: 'x-1', user: 'U2' })));
  await until(() => ownerChannels.calls.some(m => m.messageRef === 'c-2'));
  const order = ownerChannels.calls.map(m => m.messageRef);
  assert.deepEqual(order.filter(ref => ref.startsWith('c-')), ['c-1', 'c-1', 'c-1', 'c-1', 'c-2'], 'U1 in order');
  assert.ok(order.indexOf('x-1') < order.lastIndexOf('c-1'), "U2's message went on while U1's was held");
  await channel.stop();
});

test('stop ends the holding: a message the host never takes is not retried after it', async () => {
  const { channel, ownerChannels } = await started(unavailableFor(Infinity), { retryMs: 5 });
  const delivered = channel.onEnvelope(envelope(dm()));
  await until(() => ownerChannels.calls.length >= 2);
  await channel.stop();
  await delivered;
  const seen = ownerChannels.calls.length;
  await tick(30);
  assert.equal(ownerChannels.calls.length, seen);
});

test(`at most ${HELD_PER_SENDER} messages of one sender are held; past that one is logged by length and not held`, async () => {
  const warnings = [];
  const ownerChannels = { calls: [], inbound: message => (ownerChannels.calls.push(message), new Promise(() => {})) }; // never answers
  const { channel, socket } = await started(ownerChannels, { log: { info() {}, warn: line => warnings.push(line) } });
  for (let i = 0; i <= HELD_PER_SENDER; i++) socket.emit('slack_event', envelope(dm({ client_msg_id: 'c-' + i, text: 'secret' })));
  await tick(10);
  assert.equal(ownerChannels.calls.length, 1, 'one at a time per sender');
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /U1 \(6 chars\) not held/);
  assert.doesNotMatch(warnings[0], /secret/);
  socket.emit('slack_event', envelope(dm({ user: 'U2', client_msg_id: 'x-1' })));
  await tick(10);
  assert.equal(ownerChannels.calls.at(-1).senderRef, 'U2', 'another sender still goes on');
  await channel.stop();
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

/** The install that carries the pinned Slack SDK, and the paths inside it.
 *
 *  This plugin ships no SDK copy of its own (index.mjs:227-233): the DSH runtime installs
 *  it and hands its entry to loadSlack. So the SDK is resolved from an install beside the
 *  runtime, never from this test file's directory -- the node gate installs only the
 *  measured project's dependencies, so the snapshot it builds for this directory has no
 *  integrations/dsh-driver/node_modules at all. Three installs carry the same pin, in the
 *  order they are looked for: a developer's own `npm ci` in the checkout (the development
 *  fallback), the host install deploy/install_dsh_host.py creates, and the shipped image's
 *  /opt/aitelier-dsh. The first one that HAS the SDK answers; none is red, naming every
 *  place that was searched. */
function sdkInstall() {
  const roots = [
    fileURLToPath(new URL('../../dsh-driver', import.meta.url)),
    resolve(process.env.AITELIER_DSH_HOME ?? join(homedir(), '.local', 'share', 'aitelier-dsh')),
    '/opt/aitelier-dsh',
  ];
  const root = roots.find(candidate => existsSync(join(candidate, 'node_modules', '@slack', 'socket-mode', 'package.json')));
  if (!root) throw new Error(`no install carries the pinned Slack SDK; looked in ${roots.join(', ')}`);
  return {
    root,
    // createRequire resolves a bare name from the directory of the file it is given, so the
    // SDK's own package.json rather than the install's: the runtime install's top-level one
    // is not readable by the container's user, and resolving from here reaches
    // @slack/web-api as well.
    resolveFrom: join(root, 'node_modules', '@slack', 'socket-mode', 'package.json'),
    // The SDK's own source, where the state names and options this plugin relies on are written.
    socketModeSource: join(root, 'node_modules', '@slack', 'socket-mode', 'dist', 'src', 'SocketModeClient.js'),
  };
}

test('with the real SDK, a dropped socket whose reconnect Slack refuses does not reject into the process', async () => {
  // The reviewer's reproduction: with the SDK's own reconnect on, a 'close' schedules
  // start() with no catch, and invalid_auth from apps.connections.open escapes as an
  // unhandled rejection - which ends the steward's process.
  const { resolveFrom } = sdkInstall();
  const slack = loadSlack(resolveFrom);
  const { WebAPIPlatformError } = createRequire(resolveFrom)('@slack/web-api');
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
  // The manifest that pins the SDK is the runtime's, integrations/dsh-driver/package.json;
  // the install that carries it is this checkout's own `npm ci` copy, a host install, or
  // the image's runtime install (see sdkInstall). A missing install everywhere is red.
  const manifest = fileURLToPath(new URL('../../dsh-driver/package.json', import.meta.url));
  const pinned = JSON.parse(readFileSync(manifest, 'utf8')).dependencies;
  assert.equal(pinned['@slack/socket-mode'], '3.1.1');
  assert.equal(pinned['@slack/web-api'], '8.3.0');
  const { root, resolveFrom, socketModeSource } = sdkInstall();
  // The install that answers is that pin, not another Slack SDK that happens to be there.
  assert.equal(JSON.parse(readFileSync(resolveFrom, 'utf8')).version, pinned['@slack/socket-mode']);
  assert.equal(JSON.parse(readFileSync(join(root, 'node_modules', '@slack', 'web-api', 'package.json'), 'utf8')).version, pinned['@slack/web-api']);
  const slack = loadSlack(resolveFrom);
  assert.equal(typeof slack.SocketModeClient, 'function');
  assert.equal(typeof slack.WebClient, 'function');
  assert.equal(slack.logLevel, 'warn');
  const socket = new slack.SocketModeClient({ appToken: 'xapp-test', logLevel: slack.logLevel });
  // The state events status() listens to are the SDK's own names.
  const source = readFileSync(socketModeSource, 'utf8');
  for (const state of ['connected', 'disconnected']) assert.match(source, new RegExp(`= "${state}"`));
  // The option that turns the SDK's own reconnect off, by the name this plugin passes.
  assert.match(source, /autoReconnectEnabled = true,/);
  assert.match(source, /this\.emit\('slack_event', \{\s*ack,/);
  assert.equal(typeof socket.start, 'function');
  assert.equal(typeof socket.disconnect, 'function');
  const web = new slack.WebClient('xoxb-test', { logLevel: slack.logLevel });
  assert.equal(typeof web.conversations.open, 'function');
  assert.equal(typeof web.chat.postMessage, 'function');
  // Step 24.3: the upload this plugin calls, and the two files.* methods it is made of (their
  // scope, files:write, is the manifest's).
  assert.equal(typeof web.filesUploadV2, 'function');
  const webSource = readFileSync(join(root, 'node_modules', '@slack', 'web-api', 'dist', 'WebClient.js'), 'utf8');
  assert.match(webSource, /this\.files\.getUploadURLExternal\(/);
  assert.match(webSource, /this\.files\.completeUploadExternal\(/);
});

test('slack-app-manifest.json makes exactly the app this plugin needs: Socket Mode, DMs, private-channel mentions, the scopes of every call', async () => {
  const here = new URL('..', import.meta.url);
  const raw = readFileSync(new URL('slack-app-manifest.json', here), 'utf8');
  const manifest = JSON.parse(raw);
  // Socket Mode, so no public endpoint: no Request URL anywhere (a URL left over from a
  // template would send events to it instead of the socket), and no token in the file.
  assert.equal(manifest.settings.socket_mode_enabled, true);
  assert.equal(manifest.settings.token_rotation_enabled, false);
  assert.equal(manifest.settings.org_deploy_enabled, false);
  assert.doesNotMatch(raw, /request_url|https?:|xapp-|xoxb-|xoxp-/);
  // Every Web API method the plugin calls, and the bot scope Slack requires for it.
  // filesUploadV2 is the SDK's wrapper over files.getUploadURLExternal and
  // files.completeUploadExternal, both files:write.
  // Step 23 (design-steward 7.7.19): conversations.info (a group's kind: private, shared, a DM)
  // is groups:read; auth.test (the bot's own id, when an event does not name it) needs no scope.
  const methodScopes = { 'conversations.open': 'im:write', 'chat.postMessage': 'chat:write', filesUploadV2: 'files:write',
    'conversations.info': 'groups:read', 'auth.test': null };
  // Comments stripped first, so both scans below measure code: a comment that mentions the
  // web client must not count as a use of it (index.mjs carries no // or /* inside a string).
  const source = readFileSync(new URL('index.mjs', here), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  const called = [...new Set([...source.matchAll(/\bweb\.(\w+(?:\.\w+)?)\(/g)].map(([, method]) => method))];
  assert.deepEqual(called.sort(), Object.keys(methodScopes).sort(), 'a Web API call was added or removed: update methodScopes and the manifest');
  // ...and the client is used no other way (web.apiCall, web?.x.y(, a destructured
  // method): past its declaration and construction, every `web` is such a call.
  assert.deepEqual([...source.matchAll(/\bweb\b(?!\.\w+(?:\.\w+)?\(| = new WebClient\(|-api)/g)].length, 1,
    'the Web API client is used other than as web.<group>.<method>( or web.<method>(: update methodScopes and this check');
  // The one download that is not a Web API call: a file's url_private_download, with the bot
  // token, needs files:read (docs.slack.dev/messaging/working-with-files). Measured on the
  // channel, not read off its source: a DM's file is fetched from exactly its
  // url_private_download (not its url_private, nor anything else), with the bot token.
  const downloadScopes = { url_private_download: 'files:read' };
  {
    const host = mediaHost();
    const { fetch, requests } = fakeFetch({ [FILE_URL]: png });
    const { channel, socket } = await started(host, { fetch });
    await deliver(socket, envelope(dm({ subtype: 'file_share', text: '',
      files: [slackFile({ url_private: 'https://files.slack.com/files-pri/T1-F1/photo.png' })] })));
    await until(() => host.calls.length === 1);
    assert.deepEqual(requests, [{ url: FILE_URL, authorization: 'Bearer xoxb-1', redirect: 'manual' }]);
    assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }]);
    await channel.stop();
  }
  assert.ok(manifest.oauth_config.scopes.bot.includes(downloadScopes.url_private_download), 'the manifest grants the download its scope');
  // The two events the plugin takes (isOwnerDm: a message in an im; isGroupMention: a mention of
  // the bot, step 23 - never a channel's other messages, D6) and their scopes.
  const eventScopes = { 'message.im': 'im:history', app_mention: 'app_mentions:read' };
  assert.deepEqual(manifest.settings.event_subscriptions.bot_events, Object.keys(eventScopes));
  assert.ok(isOwnerDm({ type: 'message', channel_type: 'im', user: 'U1', text: 'x' }));
  assert.ok(isGroupMention({ type: 'app_mention', channel: 'C1', user: 'U1', ts: '1.2', text: `<@${BOT}> x` }));
  // Exactly the scopes those need: none missing, none extra.
  const needed = [...new Set([...Object.values(methodScopes), ...Object.values(eventScopes), ...Object.values(downloadScopes)])]
    .filter(Boolean).sort();
  assert.deepEqual([...manifest.oauth_config.scopes.bot].sort(), needed);
  // 7.7.19: no posting into channels the bot was not invited to, no group DMs, no public channels'
  // or private channels' history (un-mentioned messages are never sent to the app at all).
  for (const scope of manifest.oauth_config.scopes.bot)
    assert.doesNotMatch(scope, /^(chat:write\.public|mpim:|channels:|groups:history)/, scope);
  assert.equal(manifest.oauth_config.scopes.user, undefined);
  // People can write to the bot from its Messages tab.
  assert.equal(manifest.features.app_home.messages_tab_enabled, true);
  assert.equal(manifest.features.app_home.messages_tab_read_only_enabled, false);
  assert.equal(typeof manifest.features.bot_user.display_name, 'string');
  // No Agents & AI Apps features: the plugin does not use them, and with them Slack shows
  // the DM as threads while the plugin replies at the top of the DM.
  assert.equal(manifest.features.assistant_view, undefined);
  assert.equal(manifest.features.agent_view, undefined);
  assert.doesNotMatch(raw, /assistant|agent_/);
  assert.ok(manifest.display_information.name.length <= 35);
  assert.ok(manifest.display_information.description.length <= 140);
});


// ---- step 24.3: files (design-steward 7.8.12 "Slack") ----

const FILE_URL = 'https://files.slack.com/files-pri/T1-F1/download/photo.png';
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex');
const slackFile = (over = {}) => ({ id: 'F1', name: 'photo.png', mimetype: 'image/png', size: PNG.length, mode: 'hosted',
  is_external: false, url_private_download: FILE_URL, ...over });

/** A host's ownerChannels with storeMedia: the binding is checked before `open` (a stranger's
 *  file is never fetched), then the bytes are read whole; a refusal `open` throws is answered,
 *  any other throw is `unavailable` (lib/channels.mjs, lib/media.mjs). */
const mediaHost = ({ bound = ['U1'], answer = () => ({ accepted: true }) } = {}) => {
  const host = { calls: [], stored: [], asked: [],
    inbound: async message => (host.calls.push(message), answer(message)),
    async storeMedia(meta, open) {
      host.asked.push(meta);
      if (!bound.includes(meta.senderRef)) return { refused: 'channel_sender_not_bound' };
      try {
        const body = await open({});
        const bytes = Buffer.from(await new Response(body).arrayBuffer());
        host.stored.push({ meta, bytes });
        return { ref: `m_${String(host.stored.length).padStart(16, '0')}`, kind: meta.kind, mime: 'image/png', size: bytes.length };
      } catch (error) {
        return { refused: error?.refused ?? 'unavailable' };
      }
    } };
  return host;
};

/** A fetch that answers from `routes` (url -> a function giving a Response, or a Response),
 *  recording each request's url, authorization and redirect mode. */
const fakeFetch = (routes = {}) => {
  const requests = [];
  const fetch = async (url, init = {}) => {
    requests.push({ url: String(url), authorization: init.headers?.authorization, redirect: init.redirect });
    const route = routes[String(url)];
    if (!route) throw new TypeError('fetch failed');
    return typeof route === 'function' ? route() : route;
  };
  return { fetch, requests };
};
const png = () => new Response(PNG, { status: 200, headers: { 'content-type': 'image/png' } });

test('a file_share DM and a DM with files but no subtype both reach storeMedia, then inbound with the refs', async () => {
  for (const subtype of ['file_share', undefined]) {
    const host = mediaHost();
    const { fetch, requests } = fakeFetch({ [FILE_URL]: png });
    const { channel, socket } = await started(host, { fetch });
    await deliver(socket, envelope(dm({ subtype, text: '', files: [slackFile()] })));
    await until(() => host.calls.length === 1);
    assert.deepEqual(host.asked, [{ channel: 'slack', senderRef: 'U1', messageRef: 'c-1', index: 0, kind: 'image',
      mime: 'image/png', name: 'photo.png' }]);
    assert.deepEqual(requests, [{ url: FILE_URL, authorization: 'Bearer xoxb-1', redirect: 'manual' }]);
    assert.deepEqual(host.stored[0].bytes, PNG);
    assert.deepEqual(host.calls, [{ channel: 'slack', senderRef: 'U1', messageRef: 'c-1', text: '', at: '2025-11-18T16:00:00.000Z',
      attachments: [{ kind: 'image', ref: 'm_0000000000000001' }] }], `subtype ${subtype}`);
    await channel.stop();
  }
});

test('a file DM without any text field is still a message; a text-less DM without files is not', () => {
  assert.equal(isOwnerDm(dm({ subtype: 'file_share', text: undefined, files: [slackFile()] })), true);
  assert.equal(isOwnerDm(dm({ text: undefined, files: [] })), false);
  assert.equal(isOwnerDm(dm({ subtype: 'file_share', bot_id: 'B1', files: [slackFile()] })), false);
  assert.equal(isOwnerDm(dm({ subtype: 'file_share', channel_type: 'channel', files: [slackFile()] })), false);
});

test("a bot's file is ignored; a stranger's is never fetched and the message goes in without attachments", async () => {
  const host = mediaHost({ bound: ['U1'] });
  const { fetch, requests } = fakeFetch({ [FILE_URL]: png });
  const { channel, socket } = await started(host, { fetch });
  await deliver(socket, envelope(dm({ subtype: 'file_share', bot_id: 'B1', files: [slackFile()] })));
  await deliver(socket, envelope(dm({ user: 'U9', client_msg_id: 'c-9', subtype: 'file_share', text: 'hi',
    files: [slackFile(), slackFile({ id: 'F2' })] })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(requests, [], 'no byte of a stranger or a bot is fetched');
  assert.equal(host.asked.length, 1, 'the second file is not asked for once the sender is not bound');
  assert.deepEqual(host.calls[0], { channel: 'slack', senderRef: 'U9', messageRef: 'c-9', text: 'hi', at: '2025-11-18T16:00:00.000Z' });
  await channel.stop();
});

test('only https files.slack.com addresses are fetched: http, another host, a port, credentials or a remote file are refused unsupported', async () => {
  const host = mediaHost();
  const { fetch, requests } = fakeFetch();
  const { channel, socket } = await started(host, { fetch });
  const files = [
    slackFile({ url_private_download: 'http://files.slack.com/files-pri/T1-F1/download/a.png' }),
    slackFile({ url_private_download: 'https://files.slack.com.evil.example/a.png' }),
    slackFile({ url_private_download: 'https://evil.example/files.slack.com/a.png' }),
    slackFile({ url_private_download: 'https://files.slack.com:8443/a.png' }),
    slackFile({ url_private_download: 'https://u:p@files.slack.com/a.png' }),
    slackFile({ url_private_download: 'https://team.slack.com/files-pri/T1-F1/download/a.png' }),
    slackFile({ url_private_download: 'https://evilfiles.slack.com.attacker/a.png' }),
    slackFile({ is_external: true, mode: 'external', url_private_download: undefined, external_type: 'gdrive' }),
    slackFile({ is_external: true }),
    slackFile({ url_private_download: undefined }),
  ];
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(requests, []);
  assert.deepEqual(host.asked.map(meta => meta.index), [0],
    'refused by the channel itself: only the first goes to storeMedia, for the sender check, and is not fetched');
  assert.deepEqual(host.calls[0].attachments, files.map(() => ({ kind: 'image', refused: 'unsupported' })));
  assert.equal(slackFileUrl('/files-pri/x', FILE_URL), 'https://files.slack.com/files-pri/x');
  assert.equal(slackFileUrl(FILE_URL), FILE_URL);
  for (const sibling of ['https://team.slack.com/x', 'https://evilfiles.slack.com/x', 'https://evilfiles.slack.com.attacker/x',
    'https://slack.com/x', 'https://files.slack.com.attacker/x', 'https://slack-files.com/x'])
    assert.equal(slackFileUrl(sibling), undefined, sibling);
  assert.equal(slackFileUrl('not a url'), undefined);
  await channel.stop();
});

test('a declared size over the shared bound is too_large and a deleted file is gone, neither fetched', async () => {
  const host = mediaHost();
  const { fetch, requests } = fakeFetch({ [FILE_URL]: png });
  const { channel, socket } = await started(host, { fetch });
  const files = [
    slackFile({ size: MEDIA_LIMITS.image_bytes + 1 }),
    slackFile({ name: 'a.docm', mimetype: 'application/vnd.ms-word.document.macroEnabled.12', size: 30 * 1024 * 1024 }),
    slackFile({ name: 'clip.m4a', mimetype: 'audio/mp4', size: MEDIA_LIMITS.voice_bytes + 1 }),
    slackFile({ mode: 'tombstone' }),
    slackFile({ size: MEDIA_LIMITS.image_bytes }),
  ];
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: 'look', files })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', refused: 'too_large' }, { kind: 'file', refused: 'too_large' },
    { kind: 'voice', refused: 'too_large' }, { kind: 'image', refused: 'gone' }, { kind: 'image', ref: 'm_0000000000000001' }]);
  assert.equal(requests.length, 1, 'only the file at the bound is fetched');
  assert.deepEqual(host.asked.map(meta => meta.index), [0, 4],
    'the first for the sender check (its refusal thrown, nothing fetched), then the one at the bound: the index is its place');
  await channel.stop();
});

test('a redirect is followed only on files.slack.com; one off it is not followed and the file is refused unsupported', async () => {
  const next = 'https://files.slack.com/files-tmb/T1-F1/photo.png';
  const host = mediaHost();
  const { fetch, requests } = fakeFetch({
    [FILE_URL]: () => new Response(null, { status: 302, headers: { location: '/files-tmb/T1-F1/photo.png' } }),
    [next]: png,
    'https://files.slack.com/files-pri/T1-F2/download/b.png': () => new Response(null, { status: 302,
      headers: { location: 'https://slack-files.example/b.png' } }),
  });
  const warnings = [];
  const { channel, socket } = await started(host, { fetch, log: { info() {}, warn: line => warnings.push(line) } });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files: [slackFile(),
    slackFile({ id: 'F2', url_private_download: 'https://files.slack.com/files-pri/T1-F2/download/b.png' })] })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(requests.map(r => r.url), [FILE_URL, next, 'https://files.slack.com/files-pri/T1-F2/download/b.png']);
  assert.ok(requests.every(r => r.authorization === 'Bearer xoxb-1' && r.redirect === 'manual'));
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }, { kind: 'image', refused: 'unsupported' }]);
  assert.match(warnings.join('\n'), /redirected off files\.slack\.com/);
  assert.doesNotMatch(warnings.join('\n'), /xoxb|photo|b\.png/, 'no token and no file name in the log');
  await channel.stop();
});

test('404 is gone; a login page (no files:read) or 403 is unsupported with a hint; 503 holds the message and fetches again', async () => {
  const urls = [1, 2, 3, 4].map(n => `https://files.slack.com/files-pri/T1-F${n}/download/f.png`);
  let busy = 1;
  const host = mediaHost();
  const { fetch, requests } = fakeFetch({
    [urls[0]]: () => new Response('no', { status: 404 }),
    [urls[1]]: () => new Response('<html>sign in</html>', { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } }),
    [urls[2]]: () => new Response('no', { status: 403 }),
    [urls[3]]: () => (busy-- > 0 ? new Response('busy', { status: 503 }) : png()),
  });
  const warnings = [];
  const { channel, socket } = await started(host, { fetch, retryMs: 5, log: { info() {}, warn: line => warnings.push(line) } });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files: urls.map(url => slackFile({ url_private_download: url })) })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', refused: 'gone' }, { kind: 'image', refused: 'unsupported' },
    { kind: 'image', refused: 'unsupported' }, { kind: 'image', ref: 'm_0000000000000001' }]);
  assert.deepEqual(requests.map(r => r.url), [...urls, urls[3]], 'the stored and refused files are not fetched again on the retry');
  assert.match(warnings.join('\n'), /files:read/);
  assert.match(warnings.join('\n'), /not delivered \(attempt 1\)/);
  await channel.stop();
});

test('a host without storeMedia takes the message with its files refused unsupported, nothing fetched', async () => {
  const { fetch, requests } = fakeFetch({ [FILE_URL]: png });
  const { channel, socket, ownerChannels } = await started(registry(), { fetch });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files: [slackFile()] })));
  await until(() => ownerChannels.calls.length === 1);
  assert.deepEqual(ownerChannels.calls[0].attachments, [{ kind: 'image', refused: 'unsupported' }]);
  assert.deepEqual(requests, []);
  await channel.stop();
});

test('at most 10 files of one message are handed in (the host refuses more)', async () => {
  const host = mediaHost();
  const { fetch } = fakeFetch({ [FILE_URL]: png });
  const { channel, socket } = await started(host, { fetch });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files: Array.from({ length: 12 }, () => slackFile()) })));
  await until(() => host.calls.length === 1);
  assert.equal(host.calls[0].attachments.length, 10);
  await channel.stop();
});

test('sendFile uploads with filesUploadV2 to the DM of `to`, the caption as its escaped initial comment', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'slack-send-'));
  const path = join(dir, 'image-abc.png');
  writeFileSync(path, PNG);
  const { channel, web } = await started();
  assert.deepEqual(await channel.sendFile({ to: 'U1', kind: 'image', mime: 'image/png', name: 'image-abc.png', path, caption: 'a < b' }),
    { ref: 'F-1' });
  await channel.sendFile({ to: 'U1', replyToRef: 'c-1', kind: 'file', mime: 'application/pdf', name: 'doc.pdf', path });
  assert.deepEqual(web.opened, ['U1'], 'the DM is opened once');
  assert.equal(web.uploaded.length, 2);
  assert.deepEqual({ ...web.uploaded[0], file: undefined }, { channel_id: 'D-U1', file: undefined, filename: 'image-abc.png',
    initial_comment: 'a &lt; b' });
  assert.deepEqual(web.uploaded[0].file, PNG);
  assert.equal('initial_comment' in web.uploaded[1], false, 'no caption, no comment');
  assert.equal('channel' in web.uploaded[0] || 'channels' in web.uploaded[0], false, 'channel_id, not the deprecated channels');
});

test('sendFile refuses an unbound group, an unknown kind, no recipient, and a file over the outbound bound, uploading nothing', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'slack-send-'));
  const small = join(dir, 'a.png'), big = join(dir, 'big.bin');
  writeFileSync(small, PNG);
  writeFileSync(big, Buffer.alloc(MEDIA_LIMITS.outbound_bytes + 1));
  const { channel, web } = await started();
  // Step 23: a group's file goes only into a bound private channel (below); this one is not bound.
  await assert.rejects(channel.sendFile({ groupRef: 'C1', kind: 'image', name: 'a.png', path: small }), { code: 'room_not_bound' });
  await assert.rejects(channel.sendFile({ to: 'U1', kind: 'video', name: 'a.png', path: small }), { code: 'channel_no_media' });
  await assert.rejects(channel.sendFile({ kind: 'image', name: 'a.png', path: small }), { code: 'recipient_unknown' });
  await assert.rejects(channel.sendFile({ to: 'U1', kind: 'file', name: 'big.bin', path: big }), { code: 'too_large' });
  assert.deepEqual(web.uploaded, []);
});

test('apply registers sendFile and the three media kinds; the bounds are the media library table', () => {
  const table = JSON.parse(readFileSync(new URL('../../dsh-director/media-types.json', import.meta.url), 'utf8'));
  assert.deepEqual(MEDIA_LIMITS, table.limits);
  const registered = [];
  const dir = mkdtempSync(join(tmpdir(), 'slack-none-'));
  apply({ logger: quiet, on() {}, ownerChannels: { register: channel => (registered.push(channel), () => {}) } }, { secretsDir: dir });
  assert.equal(typeof registered[0].sendFile, 'function');
  assert.deepEqual(registered[0].mediaKinds, ['image', 'voice', 'file']);
});

test('at most 3 redirects are followed, all on files.slack.com: a file 3 hops away is stored, one 4 hops away is refused', async () => {
  const hop = (file, n) => `https://files.slack.com/files-pri/T1-${file}/hop${n}`;
  const routes = {};
  // F3: three redirects, then the file. F4: four redirects, then the file (never reached).
  for (const [file, hops] of [['F3', 3], ['F4', 4]]) {
    for (let n = 0; n < hops; n++) routes[hop(file, n)] = () => new Response(null, { status: 302, headers: { location: hop(file, n + 1) } });
    routes[hop(file, hops)] = png;
  }
  // F5: one redirect to a sibling Slack host, which is not files.slack.com.
  routes[hop('F5', 0)] = () => new Response(null, { status: 302, headers: { location: 'https://team.slack.com/files-pri/T1-F5/a.png' } });
  routes['https://team.slack.com/files-pri/T1-F5/a.png'] = png;
  const host = mediaHost();
  const { fetch, requests } = fakeFetch(routes);
  const warnings = [];
  const { channel, socket } = await started(host, { fetch, log: { info() {}, warn: line => warnings.push(line) } });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '',
    files: ['F3', 'F4', 'F5'].map(id => slackFile({ id, url_private_download: hop(id, 0) })) })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }, { kind: 'image', refused: 'unsupported' },
    { kind: 'image', refused: 'unsupported' }]);
  assert.deepEqual(requests.map(r => r.url), [0, 1, 2, 3].map(n => hop('F3', n)).concat([0, 1, 2, 3].map(n => hop('F4', n)), [hop('F5', 0)]),
    'the 4th redirect target and the sibling host are never requested');
  assert.match(warnings.join('\n'), /redirected too often/);
  assert.match(warnings.join('\n'), /redirected off files\.slack\.com/);
  await channel.stop();
});

test("a stranger's message whose files this channel refused all goes in without attachments; the owner's keeps its refusals", async () => {
  const host = mediaHost({ bound: ['U1'] });
  const { fetch, requests } = fakeFetch();
  const { channel, socket } = await started(host, { fetch });
  const files = [slackFile({ is_external: true }), slackFile({ mode: 'tombstone' }), slackFile({ size: MEDIA_LIMITS.image_bytes + 1 })];
  await deliver(socket, envelope(dm({ user: 'U9', client_msg_id: 'c-9', subtype: 'file_share', text: 'hi', files })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(host.calls[0], { channel: 'slack', senderRef: 'U9', messageRef: 'c-9', text: 'hi', at: '2025-11-18T16:00:00.000Z' },
    'no attachments, so the host writes no placeholder line for a stranger');
  assert.deepEqual(host.asked.map(meta => [meta.senderRef, meta.index]), [['U9', 0]], 'the first file asks the sender check; the rest are not asked');
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: 'hi', files })));
  await until(() => host.calls.length === 2);
  assert.deepEqual(host.calls[1].attachments, [{ kind: 'image', refused: 'unsupported' }, { kind: 'image', refused: 'gone' },
    { kind: 'image', refused: 'too_large' }]);
  assert.deepEqual(requests, [], 'nothing is fetched for either');
  assert.deepEqual(host.stored, []);
  await channel.stop();
});

test('a 204 or an empty 200 is gone for good: the message goes in at once, the file not fetched again', async () => {
  const urls = [1, 2].map(n => `https://files.slack.com/files-pri/T1-F${n}/download/f.png`);
  const host = mediaHost();
  const { fetch, requests } = fakeFetch({
    [urls[0]]: () => new Response(null, { status: 204 }),
    [urls[1]]: () => new Response(null, { status: 200, headers: { 'content-type': 'image/png' } }),
  });
  const warnings = [];
  const { channel, socket } = await started(host, { fetch, retryMs: 5, log: { info() {}, warn: line => warnings.push(line) } });
  await deliver(socket, envelope(dm({ subtype: 'file_share', text: '', files: urls.map(url => slackFile({ url_private_download: url })) })));
  await until(() => host.calls.length === 1, 1000);
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', refused: 'gone' }, { kind: 'image', refused: 'gone' }]);
  await tick(30);
  assert.deepEqual(requests.map(r => r.url), urls, 'each fetched once');
  assert.doesNotMatch(warnings.join('\n'), /not delivered/, 'not held');
  await channel.stop();
});


// ---- step 23: private channels as steward groups (design-steward 7.7.19; proposal r2 section 9) ----

const TS = '1763481600.000300';
const mention = (over = {}) => ({ type: 'app_mention', channel: 'C1', user: 'U2', text: `<@${BOT}> 武侠做到哪了`, ts: TS,
  event_ts: TS, ...over });
// An Events API envelope as Socket Mode carries it: the payload names the app's bot user
// (`authorizations`) and whether the conversation is shared with another organization.
const groupEnvelope = (event, body = {}) => {
  const acks = [];
  return { acks, ack: async () => { acks.push(true); }, envelope_id: 'e-2', type: 'events_api',
    body: { authorizations: [{ user_id: BOT, is_bot: true }], is_ext_shared_channel: false, ...body, event } };
};
const ROOMS = { ownerRef: 'U1', rooms: [{ room_ref: 'C1', prefix: `<@${BOT}>` }] };
const grouped = async (ownerChannels = registry(), extra = {}) => {
  const world = await started(ownerChannels, extra);
  await world.channel.setRooms(ROOMS);
  return world;
};
const settle = () => tick(20);
const platformError = code => Object.assign(new Error(`An API error occurred: ${code}`),
  { code: 'slack_webapi_platform_error', data: { ok: false, error: code } });

test('a mention in a bound private channel is a group question: ref <channel>:<ts>, the mention taken off, asked', async () => {
  const { channel, socket, ownerChannels, web } = await grouped();
  const env = groupEnvelope(mention({ text: `<@${BOT}> 武侠 &lt;做到&gt; 哪了` }));
  await deliver(socket, env);
  await until(() => ownerChannels.calls.length === 1);
  assert.deepEqual(ownerChannels.calls, [{ channel: 'slack', senderRef: 'U2', messageRef: `C1:${TS}`, text: '武侠 <做到> 哪了',
    at: '2025-11-18T16:00:00.000Z', conversationRef: 'C1', asked: true }]);
  assert.deepEqual(env.acks, [true]);
  assert.deepEqual(web.infoCalls, ['C1'], 'the channel is still private and not shared: asked on every message');
  // Two channels may carry the same ts: the ref is the channel's and the ts.
  await deliver(socket, groupEnvelope(mention({ channel: 'C2' })));
  await channel.setRooms({ ...ROOMS, rooms: [...ROOMS.rooms, { room_ref: 'C2', prefix: `<@${BOT}>` }] });
  await deliver(socket, groupEnvelope(mention({ channel: 'C2' })));
  await until(() => ownerChannels.calls.length === 2);
  assert.equal(ownerChannels.calls[1].messageRef, `C2:${TS}`);
  await channel.stop();
});

test('withoutMention takes the bot\'s own mentions out wherever they are, and only its own', () => {
  assert.equal(withoutMention(`<@${BOT}>  hi`, BOT), 'hi');
  assert.equal(withoutMention(`hi <@${BOT}|steward> there`, BOT), 'hi there');
  assert.equal(withoutMention(`<@U2> <@${BOT}> ask <@U2>`, BOT), '<@U2> ask <@U2>');
  assert.equal(withoutMention(`<@${BOT}>`, BOT), '');
  assert.equal(withoutMention('<@UBOTX> hi', BOT), '<@UBOTX> hi');
});

test('un-mentioned channel messages, bots, edits and incomplete mentions are ignored (D6), and acked', async () => {
  const { channel, socket, ownerChannels, web } = await grouped();
  const ignored = [
    { type: 'message', channel_type: 'group', channel: 'C1', user: 'U2', text: 'no mention', ts: TS },
    { type: 'message', channel_type: 'channel', channel: 'C1', user: 'U2', text: `<@${BOT}> in a message event`, ts: TS },
    mention({ bot_id: 'B9' }),
    mention({ subtype: 'message_changed' }),
    mention({ user: undefined }),
    mention({ channel: undefined }),
    mention({ ts: undefined }),
    mention({ text: undefined }),
    mention({ text: `<@${BOT}>` }), // nothing after the mention, no file
  ];
  const envs = ignored.map(event => groupEnvelope(event));
  for (const env of envs) await deliver(socket, env);
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  assert.deepEqual(envs.map(env => env.acks.length), envs.map(() => 1));
  assert.equal(isGroupMention(mention()), true);
  assert.equal(isGroupMention(mention({ bot_id: 'B9' })), false);
  assert.equal(web.posted.length, 0);
  await channel.stop();
});

test('a mention waits for the host\'s first room list: before it, nothing is taken or judged', async () => {
  const { channel, socket, ownerChannels, web } = await started();
  await deliver(socket, groupEnvelope(mention()));
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  assert.deepEqual(web.infoCalls, []);
  await channel.setRooms(ROOMS);
  await until(() => ownerChannels.calls.length === 1);
  assert.equal(ownerChannels.calls[0].conversationRef, 'C1');
  await channel.stop();
});

test('in a channel that is not bound only the bound account\'s group code, alone after the mention, goes in - as a bind', async () => {
  const warnings = [];
  const { channel, socket, ownerChannels, web } = await grouped(registry(), { log: { info() {}, warn: line => warnings.push(line) } });
  const code = 'G-7K2M-Q9XD';
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', user: 'U1', text: `<@${BOT}> hello` })));
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', user: 'U2', text: `<@${BOT}> ${code}` })));
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', user: 'U1', text: `<@${BOT}> ${code} please` })));
  await settle();
  assert.deepEqual(ownerChannels.calls, [], 'a member, a question or more words: dropped');
  assert.deepEqual(web.infoCalls, [], 'and not even judged');
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', user: 'U1', text: `<@${BOT}> ${code}` })));
  await until(() => ownerChannels.calls.length === 1);
  assert.deepEqual(ownerChannels.calls[0], { channel: 'slack', senderRef: 'U1', messageRef: `C9:${TS}`, text: code,
    at: '2025-11-18T16:00:00.000Z', conversationRef: 'C9', groupBind: { prefix: `<@${BOT}>`, code } });
  assert.deepEqual(web.infoCalls, ['C9']);
  assert.ok(warnings.every(line => !line.includes('hello') && !line.includes(code)), 'no text and no code in a log line');
  // Before the owner bound an account there is nobody whose code counts.
  await channel.setRooms({ ownerRef: null, rooms: [] });
  await deliver(socket, groupEnvelope(mention({ channel: 'C8', user: 'U1', text: `<@${BOT}> ${code}` })));
  await settle();
  assert.equal(ownerChannels.calls.length, 1);
  await channel.stop();
});

test('D4, D5: a group code in a public channel, a Slack Connect channel, a DM or a group DM binds nothing', async () => {
  const warnings = [];
  const { channel, socket, ownerChannels, web } = await grouped(registry(), { log: { info() {}, warn: line => warnings.push(line) } });
  const code = 'G-7K2M-Q9XD';
  web.infos = {
    C2: { id: 'C2', ...PRIVATE_CHANNEL, is_private: false },
    C3: { id: 'C3', ...PRIVATE_CHANNEL, is_ext_shared: true, is_shared: true },
    C4: { id: 'C4', ...PRIVATE_CHANNEL, is_pending_ext_shared: true },
    D5: { id: 'D5', is_im: true, is_private: true },
    G6: { id: 'G6', ...PRIVATE_CHANNEL, is_mpim: true, is_group: true },
    G7: platformError('missing_scope'),
  };
  for (const room of ['C2', 'C3', 'C4', 'D5', 'G6', 'G7'])
    await deliver(socket, groupEnvelope(mention({ channel: room, user: 'U1', text: `<@${BOT}> ${code}` })));
  // The envelope's own word is enough: a shared channel is refused before anything is asked.
  await deliver(socket, groupEnvelope(mention({ channel: 'C10', user: 'U1', text: `<@${BOT}> ${code}` }), { is_ext_shared_channel: true }));
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  assert.deepEqual([...web.infoCalls].sort(), ['C2', 'C3', 'C4', 'D5', 'G6', 'G7']);
  assert.deepEqual(web.posted, [], 'a refused code is answered with nothing');
  const reasons = Object.fromEntries(warnings.map(line => /group code in (\w+) not taken: (\w+)/.exec(line)?.slice(1) ?? [line, line]));
  assert.deepEqual(reasons, { C2: 'not_private', C3: 'shared', C4: 'shared', D5: 'not_a_channel', G6: 'not_a_channel', G7: 'unreadable',
    C10: 'shared' });
  assert.ok(warnings.every(line => !line.includes(code)));
  assert.equal(groupRefusal({ ...PRIVATE_CHANNEL }), undefined);
  assert.equal(groupRefusal(undefined), 'unreadable');
  await channel.stop();
});

test('D5: a bound channel that became shared drops its events and is paused - nothing more taken or sent', async () => {
  const warnings = [];
  const { channel, socket, ownerChannels, web } = await grouped(registry(), { log: { info() {}, warn: line => warnings.push(line) } });
  await deliver(socket, groupEnvelope(mention({ text: `<@${BOT}> secret question` }), { is_ext_shared_channel: true }));
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  assert.match(channel.status().detail, /1 group paused/);
  assert.equal(channel.status().connected, true);
  assert.ok(warnings.some(line => /C1 paused: shared/.test(line)) && warnings.every(line => !line.includes('secret')));
  // Paused: a later event (whatever the envelope says now) is dropped, a send is refused before Slack is asked.
  await deliver(socket, groupEnvelope(mention({ ts: '1763481601.000100' })));
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  await assert.rejects(channel.sendGroup('C1', 'a reminder'), { code: 'room_paused', status: 409 });
  const dir = mkdtempSync(join(tmpdir(), 'slack-group-'));
  const path = join(dir, 'a.png');
  writeFileSync(path, PNG);
  await assert.rejects(channel.sendFile({ groupRef: 'C1', kind: 'image', name: 'a.png', path }), { code: 'room_paused' });
  assert.deepEqual(web.posted, []);
  assert.deepEqual(web.uploaded, []);
  // The owner unbinds it: the host's next list no longer has it, and the pause goes with it.
  await channel.setRooms({ ownerRef: 'U1', rooms: [] });
  assert.equal(channel.status().detail, undefined);
  await channel.stop();
});

test('D4, D5: a bound channel conversations.info finds shared or public is paused too, even when the envelope did not say', async () => {
  const { channel, socket, ownerChannels, web } = await grouped();
  web.infos.C1 = { id: 'C1', ...PRIVATE_CHANNEL, is_ext_shared: true };
  await deliver(socket, groupEnvelope(mention(), { is_ext_shared_channel: undefined }));
  await settle();
  assert.deepEqual(ownerChannels.calls, []);
  await assert.rejects(channel.sendGroup('C1', 'x'), { code: 'room_paused' });
  // A send is checked as well: a channel made public since is paused before anything is posted.
  await channel.setRooms({ ...ROOMS, rooms: [...ROOMS.rooms, { room_ref: 'C2', prefix: `<@${BOT}>` }] });
  web.infos.C2 = { id: 'C2', ...PRIVATE_CHANNEL, is_private: false };
  await assert.rejects(channel.sendGroup('C2', 'a reminder'), { code: 'room_paused', status: 409 });
  assert.deepEqual(web.posted, []);
  assert.match(channel.status().detail, /2 groups paused/);
  await channel.stop();
});

test('a conversations.info failure that is not a final answer is asked again, never a pause', async () => {
  const { channel, socket, ownerChannels, web } = await grouped(registry(), { retryMs: 5 });
  let failures = 2;
  web.conversations.info = async ({ channel: room }) => {
    web.infoCalls.push(room);
    if (failures-- > 0) throw platformError('internal_error');
    return { ok: true, channel: { id: room, ...PRIVATE_CHANNEL } };
  };
  await deliver(socket, groupEnvelope(mention()));
  await until(() => ownerChannels.calls.length === 1);
  assert.deepEqual(web.infoCalls, ['C1', 'C1', 'C1']);
  assert.equal(channel.status().detail, undefined, 'not paused');
  // On a send the failure fails that send; the group is not paused.
  failures = 1;
  await assert.rejects(channel.sendGroup('C1', 'x'), /internal_error/);
  await channel.sendGroup('C1', 'y');
  assert.deepEqual(web.posted.map(p => p.text), ['y']);
  await channel.stop();
});

test('D8: the reply goes into the asker\'s thread, unfurls off, escaped, the bot\'s own mention kept as a mention', async () => {
  const { channel, socket, ownerChannels, web } = await grouped();
  await deliver(socket, groupEnvelope(mention()));
  await deliver(socket, groupEnvelope(mention({ ts: '1763481700.000100', thread_ts: '1763481500.000100' })));
  await until(() => ownerChannels.calls.length === 2);
  const answer = await channel.sendGroup('C1', `a <b> & <@U2> — 回复「<@${BOT}> 确认 K1」`, { replyToRef: `C1:${TS}` });
  assert.deepEqual(web.posted[0], { channel: 'C1', text: `a &lt;b&gt; &amp; &lt;@U2&gt; — 回复「<@${BOT}> 确认 K1」`, thread_ts: TS,
    unfurl_links: false, unfurl_media: false });
  assert.deepEqual(answer, { ref: 'C1:170000001.000100' });
  // A question asked inside a thread is answered in that thread (its parent's ts).
  await channel.sendGroup('C1', 'in the thread', { replyToRef: 'C1:1763481700.000100' });
  assert.equal(web.posted[1].thread_ts, '1763481500.000100');
  // A question from before a restart (not remembered): its own ts; a long reply stays in one thread.
  await channel.sendGroup('C1', 'x'.repeat(SLACK_TEXT_LIMIT + 1), { replyToRef: 'C1:1763400000.000100' });
  assert.deepEqual(web.posted.slice(2).map(p => [p.thread_ts, p.text.length, p.unfurl_links, p.unfurl_media]),
    [['1763400000.000100', SLACK_TEXT_LIMIT, false, false], ['1763400000.000100', 1, false, false]]);
  // A line that answers nobody (a reminder) goes into the channel itself; a ref of another channel threads nothing.
  await channel.sendGroup('C1', 'reminder');
  await channel.sendGroup('C1', 'other', { replyToRef: `C2:${TS}` });
  assert.equal('thread_ts' in web.posted.at(-2), false);
  assert.equal('thread_ts' in web.posted.at(-1), false);
  await assert.rejects(channel.sendGroup('C9', 'not bound'), { code: 'room_not_bound', status: 409 });
  await channel.stop();
});

test('a mention\'s files are stored for its room as asked, then the question goes in with the refs', async () => {
  const host = mediaHost({ bound: ['U1', 'U2'] });
  const { fetch } = fakeFetch({ [FILE_URL]: png });
  const { channel, socket } = await grouped(host, { fetch });
  await deliver(socket, groupEnvelope(mention({ text: `<@${BOT}> 这张图是什么`, files: [slackFile()] })));
  await until(() => host.calls.length === 1);
  assert.deepEqual(host.asked, [{ channel: 'slack', senderRef: 'U2', messageRef: `C1:${TS}`, index: 0, kind: 'image', mime: 'image/png',
    name: 'photo.png', conversationRef: 'C1', asked: true }]);
  assert.deepEqual(host.calls[0].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }]);
  assert.equal(host.calls[0].conversationRef, 'C1');
  await channel.stop();
});

test('sendFile into a bound private channel uploads to it (its caption escaped); an unbound one is refused', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'slack-group-'));
  const path = join(dir, 'a.png');
  writeFileSync(path, PNG);
  const { channel, web } = await grouped();
  assert.deepEqual(await channel.sendFile({ groupRef: 'C1', kind: 'image', name: 'a.png', path, caption: 'a < b' }), { ref: 'F-1' });
  assert.deepEqual({ ...web.uploaded[0], file: undefined }, { channel_id: 'C1', file: undefined, filename: 'a.png', initial_comment: 'a &lt; b' });
  assert.deepEqual(web.opened, [], 'no DM is opened for a group');
  await assert.rejects(channel.sendFile({ groupRef: 'C9', kind: 'image', name: 'a.png', path }), { code: 'room_not_bound' });
  assert.equal(web.uploaded.length, 1);
  await channel.stop();
});

test('without authorizations in the envelope the bot\'s id is asked once (auth.test)', async () => {
  const { channel, socket, ownerChannels, web } = await grouped();
  let asked = 0;
  web.auth.test = async () => (asked++, { ok: true, user_id: BOT });
  const env = groupEnvelope(mention());
  delete env.body.authorizations;
  await deliver(socket, env);
  const again = groupEnvelope(mention({ ts: '1763481601.000100' }));
  delete again.body.authorizations;
  await deliver(socket, again);
  await until(() => ownerChannels.calls.length === 2);
  assert.equal(ownerChannels.calls[0].text, '武侠做到哪了');
  assert.equal(asked, 1);
  await channel.stop();
});

test('a group\'s held question does not hold up the owner\'s DM, and a DM is never a group message', async () => {
  const ownerChannels = registry(message => (message.conversationRef ? { accepted: false, reason: 'unavailable' } : { accepted: true }));
  const { channel, socket } = await grouped(ownerChannels, { retryMs: 10 });
  await deliver(socket, groupEnvelope(mention({ user: 'U1' })));
  await deliver(socket, envelope(dm()));
  await until(() => ownerChannels.calls.some(m => m.messageRef === 'c-1'));
  const direct = ownerChannels.calls.find(m => m.messageRef === 'c-1');
  assert.equal('conversationRef' in direct, false);
  assert.equal('asked' in direct, false);
  await channel.stop();
});

test('apply registers sendGroup and setRooms for the host\'s group path', () => {
  const registered = [];
  apply({ logger: quiet, on() {}, ownerChannels: { register: channel => (registered.push(channel), () => {}) } },
    { secretsDir: mkdtempSync(join(tmpdir(), 'slack-none-')) });
  assert.equal(typeof registered[0].sendGroup, 'function');
  assert.equal(typeof registered[0].setRooms, 'function');
});

// ---- step 23 review round (rv-sg): bind replays, the paused group named, the bind notice's thread ----

test('a redelivered bind envelope after the bind took is dropped, never a group question (Socket Mode retry)', async () => {
  const code = 'G-7K2M-Q9XD';
  let channel;
  // What the director does on a bind it took: refreshRooms -> setRooms with the new room, before inbound answers.
  const ownerChannels = registry(async message => {
    if (message.groupBind) await channel.setRooms({ ownerRef: 'U1', rooms: [{ room_ref: 'C9', prefix: `<@${BOT}>`, label: '家' }] });
    return { accepted: true };
  });
  ({ channel } = await started(ownerChannels));
  const socket = FakeSocket.last;
  await channel.setRooms({ ownerRef: 'U1', rooms: [] });
  const bind = () => groupEnvelope(mention({ channel: 'C9', user: 'U1', text: `<@${BOT}> ${code}` }));
  await deliver(socket, bind());
  await until(() => ownerChannels.calls.length === 1);
  const again = bind();
  again.body.retry_attempt = 1;
  await deliver(socket, again);
  await deliver(socket, bind());
  await settle();
  assert.equal(ownerChannels.calls.length, 1, 'the same message ref is not taken a second time');
  assert.ok(ownerChannels.calls[0].groupBind);
  // A later question in the bound channel is taken as usual.
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', ts: '1763481700.000100' })));
  await until(() => ownerChannels.calls.length === 2);
  assert.equal(ownerChannels.calls[1].messageRef, 'C9:1763481700.000100');
  assert.equal(ownerChannels.calls[1].asked, true);
  await channel.stop();
});

test('D5 (a): a paused group is named in the status by the name the dashboard shows, so the owner knows which to unbind', async () => {
  const { channel, socket, web } = await started();
  await channel.setRooms({ ownerRef: 'U1', rooms: [{ room_ref: 'C1', prefix: `<@${BOT}>`, label: '家人' },
    { room_ref: 'C2', prefix: `<@${BOT}>`, label: '工作' }, { room_ref: 'C0000ABCDEF', prefix: `<@${BOT}>` }] });
  await deliver(socket, groupEnvelope(mention({ channel: 'C2' }), { is_ext_shared_channel: true }));
  await settle();
  const detail = channel.status().detail;
  assert.match(detail, /「工作」/);
  assert.doesNotMatch(detail, /家人/);
  assert.match(detail, /unbind/);
  // A group with no name is named as the dashboard names it: its ref's last six.
  web.infos.C0000ABCDEF = { id: 'C0000ABCDEF', ...PRIVATE_CHANNEL, is_private: false };
  await assert.rejects(channel.sendGroup('C0000ABCDEF', 'x'), { code: 'room_paused' });
  assert.match(channel.status().detail, /「工作」 shared/);
  assert.match(channel.status().detail, /「ABCDEF」 not_private/);
  await channel.stop();
});

test('D8: the bind notice goes into the thread the owner posted the code in (its root, not the code message)', async () => {
  const code = 'G-7K2M-Q9XD';
  let channel;
  const ownerChannels = registry(async message => {
    if (message.groupBind) await channel.setRooms({ ownerRef: 'U1', rooms: [{ room_ref: 'C9', prefix: `<@${BOT}>` }] });
    return { accepted: true };
  });
  ({ channel } = await started(ownerChannels));
  const socket = FakeSocket.last, web = FakeWeb.last;
  await channel.setRooms({ ownerRef: 'U1', rooms: [] });
  await deliver(socket, groupEnvelope(mention({ channel: 'C9', user: 'U1', text: `<@${BOT}> ${code}`, ts: '1763481700.000100',
    thread_ts: '1763481500.000100' })));
  await until(() => ownerChannels.calls.length === 1);
  await channel.sendGroup('C9', 'NOTICE', { replyToRef: 'C9:1763481700.000100' });
  assert.equal(web.posted[0].thread_ts, '1763481500.000100');
  await channel.stop();
});
