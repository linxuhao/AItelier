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
import { apply, inject, isOwnerDm, loadSlack, name, readTokens, slackChannel, splitText, SLACK_TEXT_LIMIT } from '../index.mjs';

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
  await channel.send('x < y & z', { replyToRef: 'c-1' });
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

test('a reply goes to the DM of whoever wrote the message it answers', async () => {
  const ownerChannels = registry(message => ({ accepted: message.senderRef === 'U1' }));
  const { channel, socket, web } = await started(ownerChannels);
  await deliver(socket, envelope(dm()));
  // A stranger's 6-digit bind attempt is answered on its own DM, not the owner's.
  await deliver(socket, envelope(dm({ user: 'U2', client_msg_id: 'c-2', text: '123456' })));
  assert.deepEqual(await channel.send('bound', { replyToRef: 'c-2' }), { ref: '170000001.000100' });
  assert.deepEqual(await channel.send('ok', { replyToRef: 'c-1' }), { ref: '170000002.000100' });
  assert.deepEqual(web.posted.map(p => p.channel), ['D-U2', 'D-U1']);
  assert.deepEqual(web.opened, ['U2', 'U1']);
});

test('an unprompted message goes to the last account the service accepted, and fails before there is one', async () => {
  const ownerChannels = registry(message => ({ accepted: message.senderRef === 'U1' }));
  const { channel, socket, web } = await started(ownerChannels);
  await assert.rejects(channel.send('hi'), /no owner account/);
  await deliver(socket, envelope(dm()));
  await deliver(socket, envelope(dm({ user: 'U9', client_msg_id: 'c-9' }))); // refused: not the owner
  await channel.send('reminder');
  await channel.send('again', { replyToRef: 'unknown-ref' });
  assert.deepEqual(web.posted.map(p => p.channel), ['D-U1', 'D-U1']);
  assert.deepEqual(web.opened, ['U1'], 'the DM channel is opened once and reused');
});

test('send splits text longer than 3500 characters into consecutive messages and returns the first ref', async () => {
  const { channel, socket, web } = await started();
  await deliver(socket, envelope(dm()));
  const text = 'a'.repeat(SLACK_TEXT_LIMIT) + 'b'.repeat(SLACK_TEXT_LIMIT) + 'c';
  assert.deepEqual(await channel.send(text, { replyToRef: 'c-1' }), { ref: '170000001.000100' });
  assert.deepEqual(web.posted.map(p => p.text), ['a'.repeat(SLACK_TEXT_LIMIT), 'b'.repeat(SLACK_TEXT_LIMIT), 'c']);
  await channel.send('a'.repeat(SLACK_TEXT_LIMIT), { replyToRef: 'c-1' });
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

test('status follows the socket: connecting, connected, reconnecting, disconnected', async () => {
  const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-1', botToken: 'xoxb-1',
    SocketModeClient: class extends FakeSocket { async start() {} }, WebClient: FakeWeb, log: quiet });
  assert.deepEqual(channel.status(), { connected: false, detail: 'connecting' });
  await channel.start();
  const socket = FakeSocket.last;
  socket.emit('connected');
  assert.deepEqual(channel.status(), { connected: true });
  socket.emit('reconnecting');
  assert.deepEqual(channel.status(), { connected: false, detail: 'reconnecting' });
  socket.emit('connected');
  await channel.stop();
  assert.equal(socket.disconnected, true);
  assert.deepEqual(channel.status(), { connected: false, detail: 'disconnected' });
});

test('a socket that cannot start reports why, without the token, and does not crash', async () => {
  const warnings = [];
  const failing = class extends FakeSocket {
    async start() { throw Object.assign(new Error('An API error occurred: invalid_auth'), { data: { error: 'invalid_auth' } }); }
  };
  const channel = slackChannel({ ownerChannels: registry(), appToken: 'xapp-SECRET', botToken: 'xoxb-SECRET',
    SocketModeClient: failing, WebClient: FakeWeb, log: { info() {}, warn: line => warnings.push(line) } });
  await channel.start();
  assert.deepEqual(channel.status(), { connected: false, detail: 'start failed: invalid_auth' });
  assert.doesNotMatch(warnings.join('\n'), /SECRET/);
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
  for (const state of ['connected', 'reconnecting', 'disconnected']) assert.match(source, new RegExp(`= "${state}"`));
  assert.match(source, /this\.emit\('slack_event', \{\s*ack,/);
  assert.equal(typeof socket.start, 'function');
  assert.equal(typeof socket.disconnect, 'function');
  const web = new slack.WebClient('xoxb-test', { logLevel: slack.logLevel });
  assert.equal(typeof web.conversations.open, 'function');
  assert.equal(typeof web.chat.postMessage, 'function');
});
