/** The WeChat channel plugin against a fake bridge (a loopback HTTP server that
 *  answers like wechat-bridge) and a fake ownerChannels service. One test runs it
 *  against the real bridge module with a fake Wechaty, to pin the wire format. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as plugin from './index.mjs';
import { startWechatChannel, REPLAY_WINDOW_MS } from './index.mjs';
import { createBridge } from '../wechat-bridge/bridge.mjs';

const TOKEN = 'plugin-test-token';
const dir = mkdtempSync(join(tmpdir(), 'dsh-channel-wechat-'));
const tokenFile = join(dir, 'token');
writeFileSync(tokenFile, TOKEN + '\n');
const quiet = { warn() {}, info() {}, error() {} };
const until = async (check, ms = 3000) => {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error('timed out waiting');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
};

function fakeChannels({ accept = () => true, fail = 0 } = {}) {
  const service = { registered: undefined, unregistered: false, received: [], failures: fail };
  service.register = channel => { service.registered = channel; return () => { service.unregistered = true; }; };
  service.inbound = async message => {
    service.received.push(message);
    if (service.failures > 0) { service.failures--; throw new Error('host down'); }
    return { accepted: accept(message) };
  };
  return service;
}

/** A bridge stand-in: events are pushed by the test; every request is recorded. */
async function fakeBridge({ status = { logged_in: true, since: 'x' } } = {}) {
  const bridge = { events: [], requests: [], sent: [], status, sendAnswer: [200, { ref: null }] };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://x');
    bridge.requests.push({ method: request.method, path: url.pathname, query: Object.fromEntries(url.searchParams),
      auth: request.headers.authorization });
    const reply = (code, body) => { response.writeHead(code, { 'content-type': 'application/json' }); response.end(JSON.stringify(body)); };
    if (request.headers.authorization !== `Bearer ${TOKEN}`) return reply(401, { error: 'unauthorized' });
    if (url.pathname === '/status') return reply(200, bridge.status);
    if (url.pathname === '/events') {
      const after = Number(url.searchParams.get('after'));
      const found = bridge.events.filter(e => e.seq > after);
      if (!found.length) await new Promise(resolve => setTimeout(resolve, 20));
      return reply(200, { events: found, next_after: found.length ? found.at(-1).seq : after });
    }
    if (url.pathname === '/send') {
      let raw = '';
      for await (const chunk of request) raw += chunk;
      bridge.sent.push(JSON.parse(raw));
      return reply(...bridge.sendAnswer);
    }
    return reply(404, { error: 'not_found' });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  bridge.url = `http://127.0.0.1:${server.address().port}`;
  bridge.close = () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); });
  return bridge;
}

const event = (seq, extra = {}) => ({ seq, type: 'message', sender_ref: 'wxid_owner', message_ref: 'm' + seq,
  text: 'hi ' + seq, at: new Date().toISOString(), ...extra });

test('module shape: name, inject and apply', () => {
  assert.equal(plugin.name, 'dsh-channel-wechat');
  assert.deepEqual(plugin.inject, ['ownerChannels']);
  assert.equal(typeof plugin.apply, 'function');
});

test('without a token file the channel is registered disconnected and nothing is polled', async () => {
  const channels = fakeChannels();
  let fetched = 0;
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, fetch: async () => { fetched++; throw new Error('no'); },
    config: { tokenFile: join(dir, 'absent'), url: 'http://127.0.0.1:1' } });
  assert.equal(channels.registered.name, 'wechat');
  assert.deepEqual(channels.registered.status(), { connected: false, detail: 'not configured' });
  await assert.rejects(channels.registered.send('hello', {}), /not configured/);
  await channel.stop();
  assert.equal(fetched, 0);
  assert.equal(channels.unregistered, true);
});

test('bridge messages reach ownerChannels.inbound with the channel fields, and the cursor advances', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  bridge.events.push(event(101), event(102, { text: '同意 proposal p-1' }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 2);
    assert.deepEqual(channels.received[1], { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm102',
      text: '同意 proposal p-1', at: bridge.events[1].at });
    bridge.events.push(event(103));
    await until(() => channels.received.length === 3);
    const polls = bridge.requests.filter(r => r.path === '/events');
    assert.equal(polls[0].query.after, '0');
    assert.ok(polls.some(r => r.query.after === '102'), 'polled after what it had seen');
    assert.ok(bridge.requests.every(r => r.auth === `Bearer ${TOKEN}`));
    assert.equal(channels.received.filter(m => m.messageRef === 'm101').length, 1, 'no message is passed on twice');
  } finally { await channel.stop(); await bridge.close(); }
});

test('messages from well before this start are not replayed; recent ones are', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const old = new Date(Date.now() - REPLAY_WINDOW_MS - 60_000).toISOString();
  const recent = new Date(Date.now() - 60_000).toISOString();
  bridge.events.push(event(1, { at: old }), event(2, { at: recent }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 1);
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m2']);
  } finally { await channel.stop(); await bridge.close(); }
});

test('status: connected when logged in, the QR in the detail when a scan is needed, unreachable when the bridge is down', async () => {
  const bridge = await fakeBridge({ status: { logged_in: false, qr: 'https://login.weixin.qq.com/l/q1', since: 'x' } });
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, statusEveryMs: 30, retryMs: 30 });
  try {
    await until(() => channels.registered.status().detail?.startsWith('scan needed'));
    assert.deepEqual(channels.registered.status(), { connected: false, detail: 'scan needed: https://login.weixin.qq.com/l/q1' });
    bridge.status = { logged_in: false, since: 'x' };
    await until(() => channels.registered.status().detail === 'scan needed');
    bridge.status = { logged_in: true, user_id: 'wxid_bot', since: 'x' };
    await until(() => channels.registered.status().connected);
    assert.deepEqual(channels.registered.status(), { connected: true });
    await bridge.close();
    await until(() => channels.registered.status().detail === 'bridge unreachable');
    assert.equal(channels.registered.status().connected, false);
  } finally { await channel.stop(); }
});

test('a wrong token shows as refused, not as a scan', async () => {
  const bridge = await fakeBridge();
  const wrong = join(dir, 'wrong');
  writeFileSync(wrong, 'nope');
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile: wrong, url: bridge.url },
    waitSeconds: 1, retryMs: 30 });
  try {
    await until(() => channels.registered.status().detail === 'bridge refused the token');
  } finally { await channel.stop(); await bridge.close(); }
});

test('send replies to the sender of the answered message, or to the last accepted sender when unprompted', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels({ accept: m => m.senderRef !== 'wxid_stranger' });
  bridge.events.push(event(1, { sender_ref: 'wxid_owner' }), event(2, { sender_ref: 'wxid_stranger' }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 2);
    const { send } = channels.registered;
    assert.deepEqual(await send('reply', { replyToRef: 'm1' }), {});
    assert.deepEqual(await send('unprompted', {}), {}, 'a stranger who was not accepted is never written to');
    bridge.sendAnswer = [200, { ref: 'r9' }];
    assert.deepEqual(await send('told', { to: 'wxid_named' }), { ref: 'r9' });
    assert.deepEqual(bridge.sent, [{ to: 'wxid_owner', text: 'reply' }, { to: 'wxid_owner', text: 'unprompted' },
      { to: 'wxid_named', text: 'told' }]);
    bridge.sendAnswer = [409, { error: 'recipient_unknown' }];
    await assert.rejects(send('again', { replyToRef: 'm1' }), error => error.code === 'recipient_unknown');
  } finally { await channel.stop(); await bridge.close(); }
});

test('send with no known recipient fails without calling the bridge', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await assert.rejects(channels.registered.send('hello', {}), error => error.code === 'recipient_unknown');
    assert.deepEqual(bridge.sent, []);
  } finally { await channel.stop(); await bridge.close(); }
});

test('an inbound that throws is retried, then given up, and polling goes on', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels({ fail: 5 });
  const warnings = [];
  bridge.events.push(event(1, { text: 'secret body' }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.length === 3);
    bridge.events.push(event(2));
    await until(() => channels.received.some(m => m.messageRef === 'm2'));
    assert.equal(channels.received.filter(m => m.messageRef === 'm1').length, 3);
    assert.ok(warnings.length >= 3);
    assert.ok(warnings.every(line => !line.includes('secret body')), 'the text is never logged');
  } finally { await channel.stop(); await bridge.close(); }
});

test('apply: dispose stops polling and unregisters', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const ctx = new EventEmitter();
  ctx.ownerChannels = channels;
  ctx.logger = quiet;
  plugin.apply(ctx, { tokenFile, url: bridge.url });
  try {
    await until(() => bridge.requests.some(r => r.path === '/events'));
    const disposers = ctx.listeners('dispose');
    assert.equal(disposers.length, 1);
    await disposers[0]();
    assert.equal(channels.unregistered, true);
    const seen = bridge.requests.length;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(bridge.requests.length, seen, 'no request after dispose');
  } finally { await bridge.close(); }
});

test('end to end with the real bridge module and a fake Wechaty', async () => {
  const types = { Message: { Text: 7 }, Contact: { Official: 2 }, ScanStatus: { Waiting: 2 } };
  const bot = new EventEmitter();
  const said = [];
  bot.Contact = { find: async ({ id }) => ({ id, say: async text => { said.push({ id, text }); } }) };
  const bridge = createBridge({ bot, types, token: TOKEN });
  const address = await bridge.listen(0);
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet,
    config: { tokenFile, url: `http://${address.address}:${address.port}` }, waitSeconds: 2, statusEveryMs: 20 });
  try {
    bot.emit('login', { id: 'wxid_bot' });
    await until(() => channels.registered.status().connected);
    bot.emit('message', { id: 'w1', self: () => false, room: () => undefined, type: () => 7, text: () => '你好',
      date: () => new Date(), talker: () => ({ id: 'wxid_owner', type: () => 0 }) });
    await until(() => channels.received.length === 1);
    assert.deepEqual({ ...channels.received[0], at: undefined },
      { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'w1', text: '你好', at: undefined });
    assert.deepEqual(await channels.registered.send('收到', { replyToRef: 'w1' }), {});
    assert.deepEqual(said, [{ id: 'wxid_owner', text: '收到' }]);
  } finally { await channel.stop(); await bridge.close(); }
});
