/** The WeChat channel plugin against a fake bridge (a loopback HTTP server that
 *  answers like wechat-bridge) and a fake ownerChannels service. One test runs it
 *  against the real bridge module with a fake Wechaty, to pin the wire format. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync, truncateSync } from 'node:fs';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as plugin from './index.mjs';
import { startWechatChannel, statusFromBridge } from './index.mjs';
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

/** `fail` inbounds throw; then `unavailable` inbounds answer like the real host does
 *  when it or the backend cannot be reached; then `answer` decides. */
function fakeChannels({ accept = () => true, fail = 0, unavailable = 0, answer } = {}) {
  const service = { registered: undefined, unregistered: false, received: [], failures: fail, unavailable };
  service.register = channel => { service.registered = channel; return () => { service.unregistered = true; }; };
  service.inbound = async message => {
    service.received.push(message);
    if (service.failures > 0) { service.failures--; throw new Error('host down'); }
    if (service.unavailable > 0) { service.unavailable--; return { accepted: false, reason: 'unavailable' }; }
    return answer ? answer(message) : { accepted: accept(message) };
  };
  return service;
}

/** A bridge stand-in: events are pushed by the test; every request is recorded. */
async function fakeBridge({ status = { logged_in: true, since: 'x' }, hangEvents = false } = {}) {
  // Step 24.2: `media` maps a media_id to its answers, used in turn (the last one stays);
  // `files` records each POST /send-file (its query and body).
  const bridge = { events: [], requests: [], sent: [], rooms: [], status, sendAnswer: [200, { ref: null }], loginAnswer: [202, { phase: 'starting' }],
    media: new Map(), files: [], sendFileAnswer: [200, { ref: 'f-1', sent_as: 'image' }] };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://x');
    bridge.requests.push({ method: request.method, path: url.pathname, query: Object.fromEntries(url.searchParams),
      auth: request.headers.authorization });
    const reply = (code, body) => { response.writeHead(code, { 'content-type': 'application/json' }); response.end(JSON.stringify(body)); };
    if (request.headers.authorization !== `Bearer ${TOKEN}`) return reply(401, { error: 'unauthorized' });
    if (url.pathname === '/status') return reply(200, bridge.status);
    if (url.pathname === '/events') {
      if (hangEvents) return; // accepted, never answered
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
    if (url.pathname === '/login' && request.method === 'POST') return reply(...bridge.loginAnswer);
    if (url.pathname.startsWith('/media/') && request.method === 'GET') {
      const answers = bridge.media.get(decodeURIComponent(url.pathname.slice(7)));
      if (!answers) return reply(404, { error: 'not_found' });
      const [code, bytes] = answers.length > 1 ? answers.shift() : answers[0];
      if (code !== 200) return reply(code, { error: String(code) });
      response.writeHead(200, { 'content-type': 'application/octet-stream' });
      return response.end(bytes);
    }
    if (url.pathname === '/send-file' && request.method === 'POST') {
      const parts = [];
      for await (const chunk of request) parts.push(chunk);
      bridge.files.push({ query: Object.fromEntries(url.searchParams), bytes: Buffer.concat(parts),
        type: request.headers['content-type'] });
      return reply(...bridge.sendFileAnswer);
    }
    if (url.pathname === '/rooms' && request.method === 'PUT') {
      let raw = '';
      for await (const chunk of request) raw += chunk;
      bridge.rooms.push(JSON.parse(raw));
      return reply(200, { rooms: 1 });
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

test('without a url the bridge is looked for on 127.0.0.1:18741 (run.sh\'s host port), or on WECHAT_BRIDGE_PORT', async () => {
  const saved = process.env.WECHAT_BRIDGE_PORT;
  const urls = [];
  const fetch = async url => { urls.push(String(url)); throw new Error('no bridge here'); };
  try {
    for (const port of [undefined, '19999']) {
      if (port === undefined) delete process.env.WECHAT_BRIDGE_PORT; else process.env.WECHAT_BRIDGE_PORT = port;
      const before = urls.length;
      const channel = startWechatChannel({ ownerChannels: fakeChannels(), logger: quiet, fetch, config: { tokenFile }, waitSeconds: 1, retryMs: 10 });
      await until(() => urls.length > before);
      await channel.stop();
      assert.match(urls[before], new RegExp(`^http://127\\.0\\.0\\.1:${port ?? 18741}/`));
    }
  } finally {
    if (saved === undefined) delete process.env.WECHAT_BRIDGE_PORT; else process.env.WECHAT_BRIDGE_PORT = saved;
  }
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

// ---- step 23: the family group (design-steward 7.7.11, 7.7.16 bridge and plugin tests) ----

test('a bridge group event reaches ownerChannels.inbound with its room; a group code carries groupBind; a refused group message never holds the cursor, `unavailable` does', async () => {
  const bridge = await fakeBridge();
  let down = 2;
  const channels = fakeChannels({ answer: m => {
    if (m.messageRef === 'g3') return { accepted: false, reason: 'member_quota' };
    if (m.messageRef === 'g4' && down > 0) { down--; return { accepted: false, reason: 'unavailable' }; }
    return { accepted: true };
  } });
  const at = new Date().toISOString();
  bridge.events.push({ seq: 1, type: 'group_message', room_ref: 'r1@chatroom', sender_ref: 'wxid_m1', message_ref: 'g1', text: '周六干嘛', at },
    { seq: 2, type: 'group_bind', room_ref: 'r2@chatroom', sender_ref: 'wxid_owner', message_ref: 'g2', prefix: '@管家', code: 'G-7K2M-Q9XD', at },
    { seq: 3, type: 'group_message', room_ref: 'r1@chatroom', sender_ref: 'wxid_m1', message_ref: 'g3', text: '又问', at },
    { seq: 4, type: 'group_message', room_ref: 'r1@chatroom', sender_ref: 'wxid_m2', message_ref: 'g4', text: '再问', at },
    event(5));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.some(m => m.messageRef === 'm5'));
    assert.deepEqual(channels.received[0], { channel: 'wechat', senderRef: 'wxid_m1', messageRef: 'g1', text: '周六干嘛', at,
      conversationRef: 'r1@chatroom' });
    assert.deepEqual(channels.received[1], { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'g2', text: 'G-7K2M-Q9XD', at,
      conversationRef: 'r2@chatroom', groupBind: { prefix: '@管家', code: 'G-7K2M-Q9XD' } });
    // member_quota is final (once); `unavailable` holds g4 until the host answers, in order.
    assert.deepEqual(channels.received.map(m => m.messageRef), ['g1', 'g2', 'g3', 'g4', 'g4', 'g4', 'm5']);
    assert.equal(channels.received[6].conversationRef, undefined, 'a 1:1 message carries no room');
  } finally { await channel.stop(); await bridge.close(); }
});

test('sendGroup posts {room, text} to /send; setRooms puts the whole whitelist to /rooms', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.registered !== undefined);
    bridge.sendAnswer = [200, { ref: 'g-out-1' }];
    assert.deepEqual(await channels.registered.sendGroup('r1@chatroom', '「周六干嘛」\n去外婆家'), { ref: 'g-out-1' });
    assert.deepEqual(bridge.sent, [{ room: 'r1@chatroom', text: '「周六干嘛」\n去外婆家' }]);
    await channels.registered.setRooms({ ownerRef: 'wxid_owner', rooms: [{ room_ref: 'r1@chatroom', prefix: '@管家' }] });
    await channels.registered.setRooms({ rooms: [] });
    assert.deepEqual(bridge.rooms, [{ owner_ref: 'wxid_owner', rooms: [{ room_ref: 'r1@chatroom', prefix: '@管家' }] },
      { owner_ref: null, rooms: [] }]);
    assert.ok(bridge.requests.filter(r => r.path === '/rooms').every(r => r.method === 'PUT' && r.auth === `Bearer ${TOKEN}`));
  } finally { await channel.stop(); await bridge.close(); }
});

// Ten minutes was the old cutoff (review races_f4): nothing is dropped for its age now.
const LONG_AGO = 15 * 60_000;

test('after an outage the bridge\'s old messages are all passed on, in order: the host takes each ref once and dates an approval itself', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const old = new Date(Date.now() - LONG_AGO).toISOString();
  const recent = new Date(Date.now() - 12_000).toISOString();
  bridge.events.push(event(1, { at: old, text: 'an ordinary question' }), event(2, { at: old, text: '同意 proposal p-1' }),
    event(3, { at: recent }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 3);
    assert.deepEqual(channels.received.map(m => [m.messageRef, m.at]), [['m1', old], ['m2', old], ['m3', recent]],
      'each with the time the owner wrote it, for the host to judge');
  } finally { await channel.stop(); await bridge.close(); }
});

test('status: connected when logged in, the drawn QR (never its payload) when a scan is needed, unreachable when the bridge is down', async () => {
  const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M4 4.5h7"/></svg>';
  const bridge = await fakeBridge({ status: { logged_in: false, qr: 'https://login.weixin.qq.com/l/q1', qr_svg: SVG, since: 'x' } });
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, statusEveryMs: 30, retryMs: 30 });
  try {
    await until(() => channels.registered.status().qrSvg);
    assert.deepEqual(channels.registered.status(), { connected: false, detail: 'scan needed', qrSvg: SVG });
    bridge.status = { logged_in: false, since: 'x' };
    await until(() => !channels.registered.status().qrSvg);
    assert.deepEqual(channels.registered.status(), { connected: false, detail: 'scan needed' });
    bridge.status = { logged_in: true, user_id: 'wxid_bot', since: 'x' };
    await until(() => channels.registered.status().connected);
    assert.deepEqual(channels.registered.status(), { connected: true });
    await bridge.close();
    await until(() => channels.registered.status().detail === 'bridge unreachable');
    assert.equal(channels.registered.status().connected, false);
  } finally { await channel.stop(); }
});

test('status follows the bridge\'s phase: idle with its reason, starting, the QR while waiting, scanned', () => {
  const SVG = '<svg xmlns="http://www.w3.org/2000/svg"/>';
  assert.deepEqual(statusFromBridge({ phase: 'idle', idle_reason: 'qr_expired', logged_in: false }),
    { connected: false, detail: 'no QR (qr_expired) - request one on the dashboard' });
  assert.deepEqual(statusFromBridge({ phase: 'starting', logged_in: false }), { connected: false, detail: 'QR requested' });
  assert.deepEqual(statusFromBridge({ phase: 'waiting_qr', logged_in: false, qr: 'https://login.weixin.qq.com/l/x', qr_svg: SVG }),
    { connected: false, detail: 'scan needed', qrSvg: SVG }, 'the drawing, never the payload');
  assert.deepEqual(statusFromBridge({ phase: 'scanned', logged_in: false }), { connected: false, detail: 'QR scanned - confirm on the phone' });
  assert.deepEqual(statusFromBridge({ phase: 'logged_in', logged_in: true }), { connected: true });
  // A bridge from before the on-demand QR has no phase: read as it always was.
  assert.deepEqual(statusFromBridge({ logged_in: false, qr_svg: SVG }), { connected: false, detail: 'scan needed', qrSvg: SVG });
  assert.deepEqual(statusFromBridge({ logged_in: false }), { connected: false, detail: 'scan needed' });
});

test('login asks the bridge for a QR, answers without it, and reads the status quickly for a while', async () => {
  const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M4 4.5h7"/></svg>';
  const bridge = await fakeBridge({ status: { phase: 'idle', idle_reason: 'boot', logged_in: false, since: 'x' } });
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, statusEveryMs: 60_000, fastEveryMs: 20, fastForMs: 400 });
  try {
    await until(() => channels.registered.status().detail?.startsWith('no QR'));
    bridge.status = { phase: 'starting', logged_in: false, since: 'x' };
    assert.deepEqual(await channels.registered.login(), { requested: true });
    assert.equal(bridge.requests.filter(r => r.path === '/login').length, 1);
    assert.equal(bridge.requests.find(r => r.path === '/login').method, 'POST');
    assert.equal(channels.registered.status().detail, 'QR requested', 'read right after the request');
    bridge.status = { phase: 'waiting_qr', logged_in: false, qr: 'https://login.weixin.qq.com/l/SECRET', qr_svg: SVG, since: 'x' };
    await until(() => channels.registered.status().qrSvg === SVG, 1000);
    // The fast reads end after fastForMs.
    await new Promise(resolve => setTimeout(resolve, 500));
    const reads = bridge.requests.filter(r => r.path === '/status').length;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(bridge.requests.filter(r => r.path === '/status').length, reads, 'no fast reads after the window');
  } finally { await channel.stop(); await bridge.close(); }
});

test('login passes the bridge\'s refusals on, and names an old bridge, a wrong token, an unreachable bridge and a missing token', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    for (const refused of ['already_logged_in', 'qr_waiting', 'login_starting', 'login_disabled']) {
      bridge.loginAnswer = [409, { refused }];
      assert.deepEqual(await channels.registered.login(), { refused });
    }
    bridge.loginAnswer = [429, { refused: 'rate_limited', retry_after_s: 95 }];
    assert.deepEqual(await channels.registered.login(), { refused: 'rate_limited', retry_after_s: 95 });
    bridge.loginAnswer = [404, { error: 'not_found' }];
    assert.deepEqual(await channels.registered.login(), { refused: 'not_supported' });
  } finally { await channel.stop(); }
  const wrong = join(dir, 'wrong-login');
  writeFileSync(wrong, 'nope');
  const refused = fakeChannels();
  const other = startWechatChannel({ ownerChannels: refused, logger: quiet, config: { tokenFile: wrong, url: bridge.url }, waitSeconds: 1 });
  try { assert.deepEqual(await refused.registered.login(), { refused: 'bridge_refused_token' }); }
  finally { await other.stop(); }
  await bridge.close();
  const down = fakeChannels();
  const gone = startWechatChannel({ ownerChannels: down, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 30 });
  try { assert.deepEqual(await down.registered.login(), { refused: 'bridge_unreachable' }); }
  finally { await gone.stop(); }
  const none = fakeChannels();
  const unconfigured = startWechatChannel({ ownerChannels: none, logger: quiet, config: { tokenFile: join(dir, 'absent'), url: bridge.url } });
  try { assert.deepEqual(await none.registered.login(), { refused: 'not_configured' }); }
  finally { await unconfigured.stop(); }
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

test('send writes to the recipient the host names, and never without one', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  bridge.events.push(event(1, { sender_ref: 'wxid_owner' }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 1);
    const { send } = channels.registered;
    // An accepted sender is not remembered as a recipient: the binding may change.
    for (const options of [{ replyToRef: 'm1' }, {}, undefined, { to: '' }]) {
      await assert.rejects(send('no recipient', options), error => error.code === 'recipient_unknown');
    }
    assert.deepEqual(bridge.sent, []);
    assert.deepEqual(await send('reply', { replyToRef: 'm1', to: 'wxid_owner' }), {});
    bridge.sendAnswer = [200, { ref: 'r9' }];
    assert.deepEqual(await send('told', { to: 'wxid_named' }), { ref: 'r9' });
    assert.deepEqual(bridge.sent, [{ to: 'wxid_owner', text: 'reply' }, { to: 'wxid_named', text: 'told' }]);
    bridge.sendAnswer = [409, { error: 'recipient_unknown' }];
    await assert.rejects(send('again', { to: 'wxid_owner' }), error => error.code === 'recipient_unknown');
  } finally { await channel.stop(); await bridge.close(); }
});

test('an inbound the host answers `unavailable` is held and retried, and nothing overtakes it', async () => {
  const bridge = await fakeBridge();
  // The real host (dsh-director channels.inbound) never throws: it answers this.
  const channels = fakeChannels({ unavailable: 2 });
  const warnings = [];
  bridge.events.push(event(1, { text: '同意 proposal p-1' }), event(2));
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.some(m => m.messageRef === 'm2'));
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m1', 'm1', 'm1', 'm2'], 'm1 kept until accepted, in order');
    const polls = bridge.requests.filter(r => r.path === '/events').map(r => r.query.after);
    assert.equal(polls[0], '0');
    assert.ok(!polls.includes('1'), 'the cursor never moved past the held message on its own');
    assert.equal(warnings.length, 2);
    assert.ok(warnings.every(line => !line.includes('同意')), 'the text is never logged');
  } finally { await channel.stop(); await bridge.close(); }
});

test('an inbound that throws is retried the same way', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels({ fail: 3 });
  const warnings = [];
  bridge.events.push(event(1, { text: 'secret body' }), event(2));
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.some(m => m.messageRef === 'm2'));
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m1', 'm1', 'm1', 'm1', 'm2']);
    assert.ok(warnings.every(line => !line.includes('secret body')), 'the text is never logged');
  } finally { await channel.stop(); await bridge.close(); }
});

test('a transient refusal (rate_limited, approval_pending) is held and retried: the cursor does not move past it', async () => {
  // The replay of the bridge's ring after an outage is a burst, and the backend takes six
  // messages a channel a minute: the seventh is refused rate_limited (and a delivery that
  // overlaps a still-running decision is answered approval_pending). Neither is final.
  const bridge = await fakeBridge();
  const reasons = ['rate_limited', 'approval_pending'];
  const channels = fakeChannels({ answer: () => (reasons.length ? { accepted: false, reason: reasons.shift() } : { accepted: true }) });
  bridge.events.push(event(1, { text: 'written during the outage' }), event(2));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.some(m => m.messageRef === 'm2'));
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m1', 'm1', 'm1', 'm2'], 'm1 kept until accepted, in order');
    const polls = bridge.requests.filter(r => r.path === '/events').map(r => r.query.after);
    assert.ok(!polls.includes('1'), 'the cursor never moved past the held message on its own');
  } finally { await channel.stop(); await bridge.close(); }
});

test('a refusal other than `unavailable` is final: not retried', async () => {
  const bridge = await fakeBridge();
  // invalid_request: what the host answers a deterministic 4xx with (a field over its bound) -
  // retrying it would hold the lane for good and fail the same way every time.
  const refusals = { m1: 'channel_sender_not_bound', m2: 'invalid_request' };
  const channels = fakeChannels({ answer: m => refusals[m.messageRef]
    ? { accepted: false, reason: refusals[m.messageRef] } : { accepted: true } });
  bridge.events.push(event(1), event(2), event(3));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.length === 3);
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m1', 'm2', 'm3']);
  } finally { await channel.stop(); await bridge.close(); }
});

test('a held message is never given up for its age: it goes on when the host is back, however long that took', async () => {
  const bridge = await fakeBridge();
  let down = true;
  const channels = fakeChannels({ answer: () => (down ? { accepted: false, reason: 'unavailable' } : { accepted: true }) });
  const warnings = [];
  // Written long ago and held while the host is down: still delivered, and the next after it.
  bridge.events.push(event(1, { at: new Date(Date.now() - LONG_AGO).toISOString() }), event(2));
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10, maxRetryMs: 20 });
  try {
    await until(() => channels.received.length >= 4);
    assert.ok(channels.received.every(m => m.messageRef === 'm1'), 'm1 held, nothing overtakes it');
    down = false;
    await until(() => channels.received.some(m => m.messageRef === 'm2'));
    assert.deepEqual([...new Set(channels.received.map(m => m.messageRef))], ['m1', 'm2']);
    assert.ok(warnings.every(line => line.includes('not delivered')), 'no message is reported dropped');
  } finally { await channel.stop(); await bridge.close(); }
});

test('a message without a time is passed on without one (the host refuses it as an approval), never stamped now', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  bridge.events.push(event(1, { at: undefined, text: '同意 proposal p-1' }));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 1);
    assert.equal(channels.received[0].at, undefined);
  } finally { await channel.stop(); await bridge.close(); }
});

test('a poll the bridge accepts and never answers is given up and polled again', async () => {
  const bridge = await fakeBridge({ hangEvents: true });
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url },
    waitSeconds: 1, pollGraceMs: 50, retryMs: 20 });
  try {
    await until(() => bridge.requests.filter(r => r.path === '/events').length >= 2, 4000);
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
  bot.start = async () => {};
  bot.stop = async () => {};
  bot.Contact = { find: async ({ id }) => ({ id, say: async text => { said.push({ id, text }); } }) };
  bot.Room = { load: room => ({ say: async text => { said.push({ room, text }); return { id: 'room-out-1' }; } }) };
  const SVG = '<svg xmlns="http://www.w3.org/2000/svg"/>';
  // The bridge builds a bot per login request; this test's one fake is that bot.
  const bridge = createBridge({ makeBot: () => bot, types, token: TOKEN, renderQr: () => SVG });
  const address = await bridge.listen(0);
  const channels = fakeChannels();
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet,
    config: { tokenFile, url: `http://${address.address}:${address.port}` }, waitSeconds: 2, statusEveryMs: 20 });
  try {
    await until(() => channels.registered.status().detail === 'no QR (boot) - request one on the dashboard');
    assert.deepEqual(await channels.registered.login(), { requested: true });
    bot.emit('scan', 'https://login.weixin.qq.com/l/SECRET', 2);
    await until(() => channels.registered.status().qrSvg === SVG);
    assert.deepEqual(await channels.registered.login(), { refused: 'qr_waiting' });
    bot.emit('login', { id: 'wxid_bot' });
    await until(() => channels.registered.status().connected);
    // The live puppet's message shape (neo 0.1.1): the room id rides on `payload.roomId`,
    // and `message.room()` throws `this.roomRawPayload is not a function`. The bridge reads
    // the payload, so a bridge that still loaded a Room would lose both messages here.
    const live = fields => ({ self: () => false, type: () => 7, date: () => new Date(),
      room: () => { throw new TypeError('this.roomRawPayload is not a function'); }, ...fields });
    bot.emit('message', live({ id: 'w1', payload: {}, text: () => '你好',
      talker: () => ({ id: 'wxid_owner', type: () => 0 }) }));
    await until(() => channels.received.length === 1);
    assert.deepEqual({ ...channels.received[0], at: undefined },
      { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'w1', text: '你好', at: undefined });
    assert.deepEqual(await channels.registered.send('收到', { replyToRef: 'w1', to: 'wxid_owner' }), {});
    assert.deepEqual(said, [{ id: 'wxid_owner', text: '收到' }]);
    // Step 23: the whitelist from the host, then one family member's @-message in that room
    // and the steward's answer back into it.
    await channels.registered.setRooms({ ownerRef: 'wxid_owner', rooms: [{ room_ref: 'r1@chatroom', prefix: '@管家' }] });
    bot.emit('message', live({ id: 'w2', payload: { roomId: 'r1@chatroom' }, text: () => '@管家 周六干嘛',
      talker: () => ({ id: 'wxid_m1', type: () => 0 }) }));
    await until(() => channels.received.length === 2);
    assert.deepEqual({ ...channels.received[1], at: undefined },
      { channel: 'wechat', senderRef: 'wxid_m1', messageRef: 'w2', text: '周六干嘛', at: undefined, conversationRef: 'r1@chatroom' });
    assert.deepEqual(await channels.registered.sendGroup('r1@chatroom', '去外婆家'), { ref: 'room-out-1' });
    assert.deepEqual(said.at(-1), { room: 'r1@chatroom', text: '去外婆家' });
  } finally { await channel.stop(); await bridge.close(); }
});

// ---- step 24.2: media (design-steward 7.8.12 "微信" 交给宿主 / 发) ----

/** fakeChannels plus a storeMedia that behaves like the host's (dsh-director channels.mjs):
 *  the binding check before `open` (channel_sender_not_bound, nothing fetched), idempotent
 *  by (messageRef, index), `open` read whole, its {refused: too_large|gone} passed on and any
 *  other throw `unavailable` (transient, not kept). `log` records storeMedia and inbound in order. */
function mediaChannels({ bound = () => true, ...options } = {}) {
  const service = fakeChannels(options);
  Object.assign(service, { stored: [], opened: [], log: [] });
  const kept = new Map();
  let n = 0;
  const inbound = service.inbound;
  service.inbound = async message => { service.log.push('inbound ' + message.messageRef); return inbound(message); };
  service.storeMedia = async (meta, open) => {
    service.stored.push(meta);
    service.log.push(`store ${meta.messageRef}.${meta.index}`);
    if (!bound(meta)) return { refused: 'channel_sender_not_bound' };
    const key = `${meta.messageRef}/${meta.index}`;
    if (kept.has(key)) return kept.get(key);
    let bytes;
    try {
      const parts = [];
      for await (const chunk of await open({ signal: AbortSignal.timeout(5000) })) parts.push(Buffer.from(chunk));
      bytes = Buffer.concat(parts);
    } catch (error) {
      if (!['too_large', 'gone'].includes(error?.refused)) return { refused: 'unavailable' };
      kept.set(key, { refused: error.refused });
      return { refused: error.refused };
    }
    service.opened.push({ ...meta, bytes });
    const answer = { ref: 'm_' + (++n).toString(16).padStart(16, '0'), kind: meta.kind, mime: 'application/octet-stream', size: bytes.length };
    kept.set(key, answer);
    return answer;
  };
  return service;
}

/** Every inbound the plugin made: no attachments, or each exactly one m_ ref or one refusal of
 *  the host's closed set - never a bridge media_id, never channel_sender_not_bound. */
function assertClosedSet(received) {
  for (const message of received) {
    if (!('attachments' in message)) continue;
    assert.ok(message.attachments.length > 0);
    for (const item of message.attachments) {
      assert.ok(['image', 'voice', 'file'].includes(item.kind));
      assert.equal(['ref', 'refused'].filter(key => key in item).length, 1, JSON.stringify(item));
      if ('ref' in item) assert.match(item.ref, /^m_[0-9a-f]{16}$/);
      else assert.ok(['too_large', 'type', 'unsupported', 'gone'].includes(item.refused), item.refused);
      assert.ok(!('media_id' in item));
    }
  }
}

const mediaEvent = (seq, attachments, extra = {}) => event(seq, { text: '', attachments, ...extra });
const mediaFetches = bridge => bridge.requests.filter(r => r.path.startsWith('/media/')).map(r => r.path.slice(7));

test('media: a message\'s files are stored first (storeMedia, streamed from GET /media), then inbound gets only m_ refs', async () => {
  const bridge = await fakeBridge();
  const channels = mediaChannels();
  bridge.media.set('5.0', [[200, Buffer.from('JPEG-BYTES')]]);
  bridge.events.push(mediaEvent(5, [{ media_id: '5.0', kind: 'image', size: 10 }]),
    mediaEvent(6, [{ media_id: '6.0', kind: 'file', name: '报告.pdf', mime: 'application/pdf', size: 4 }]));
  bridge.media.set('6.0', [[200, Buffer.from('%PDF')]]);
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 2);
    assert.deepEqual(channels.log, ['store m5.0', 'inbound m5', 'store m6.0', 'inbound m6'], 'stored before inbound');
    assert.deepEqual(channels.stored, [
      { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm5', index: 0, kind: 'image' },
      { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm6', index: 0, kind: 'file', mime: 'application/pdf', name: '报告.pdf' }]);
    assert.deepEqual(channels.opened.map(o => o.bytes.toString()), ['JPEG-BYTES', '%PDF']);
    assert.deepEqual(channels.received[0], { channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm5', text: '',
      at: bridge.events[0].at, attachments: [{ kind: 'image', ref: 'm_0000000000000001' }] });
    assert.deepEqual(channels.received[1].attachments, [{ kind: 'file', ref: 'm_0000000000000002' }]);
    assert.ok(bridge.requests.filter(r => r.path.startsWith('/media/')).every(r => r.auth === `Bearer ${TOKEN}`));
    assertClosedSet(channels.received);
  } finally { await channel.stop(); await bridge.close(); }
});

test('media: too_large and gone descriptions are refused without storeMedia or a fetch; the bridge\'s 413/410/404 become refusals', async () => {
  const bridge = await fakeBridge();
  const channels = mediaChannels();
  bridge.media.set('2.0', [[413]]);
  bridge.media.set('3.0', [[410]]);
  bridge.events.push(
    mediaEvent(1, [{ kind: 'file', size: 30 * 1048576, too_large: true }, { kind: 'image', gone: true }, { kind: 'voice' }]),
    mediaEvent(2, [{ media_id: '2.0', kind: 'file' }]),
    mediaEvent(3, [{ media_id: '3.0', kind: 'image' }]),
    mediaEvent(4, [{ media_id: '4.0', kind: 'voice' }])); // not in the bridge: 404
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 4);
    assert.deepEqual(channels.received.map(m => m.attachments), [
      [{ kind: 'file', refused: 'too_large' }, { kind: 'image', refused: 'gone' }, { kind: 'voice', refused: 'unsupported' }],
      [{ kind: 'file', refused: 'too_large' }],
      [{ kind: 'image', refused: 'gone' }],
      [{ kind: 'voice', refused: 'gone' }]]);
    assert.deepEqual(channels.stored.map(m => m.messageRef), ['m2', 'm3', 'm4'], 'the described refusals never reach storeMedia');
    assert.deepEqual(mediaFetches(bridge), ['2.0', '3.0', '4.0']);
    assertClosedSet(channels.received);
  } finally { await channel.stop(); await bridge.close(); }
});

test('media: a busy bridge (503) holds the message and backs off; a file already stored is not fetched again', async () => {
  const bridge = await fakeBridge();
  const channels = mediaChannels();
  const warnings = [];
  bridge.media.set('1.0', [[200, Buffer.from('ONE')]]);
  bridge.media.set('1.1', [[503], [503], [200, Buffer.from('TWO')]]);
  bridge.events.push(mediaEvent(1, [{ media_id: '1.0', kind: 'image' }, { media_id: '1.1', kind: 'image' }]), event(2));
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.length === 2);
    assert.deepEqual(channels.received.map(m => m.messageRef), ['m1', 'm2'], 'nothing overtook the held message');
    assert.deepEqual(channels.received[0].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }, { kind: 'image', ref: 'm_0000000000000002' }]);
    assert.deepEqual(channels.stored.map(m => m.index), [0, 1, 1, 1], 'index 0 stored once; index 1 asked again after each 503');
    assert.deepEqual(mediaFetches(bridge), ['1.0', '1.1', '1.1', '1.1']);
    assert.equal(warnings.length, 2);
    assertClosedSet(channels.received);
  } finally { await channel.stop(); await bridge.close(); }
});

test('media: an inbound held after its files were stored is retried with the same refs, nothing stored again', async () => {
  const bridge = await fakeBridge();
  const channels = mediaChannels({ unavailable: 2 });
  bridge.media.set('1.0', [[200, Buffer.from('ONE')]]);
  bridge.events.push(mediaEvent(1, [{ media_id: '1.0', kind: 'voice', mime: 'audio/wav' }]));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1, retryMs: 10 });
  try {
    await until(() => channels.received.length === 3);
    assert.deepEqual(channels.received.map(m => m.attachments), Array(3).fill([{ kind: 'voice', ref: 'm_0000000000000001' }]));
    assert.equal(channels.stored.length, 1);
    assert.deepEqual(mediaFetches(bridge), ['1.0']);
  } finally { await channel.stop(); await bridge.close(); }
});

test('media: channel_sender_not_bound stops the fetching; inbound goes without attachments (no key at all)', async () => {
  const bridge = await fakeBridge();
  const channels = mediaChannels({ bound: meta => meta.senderRef === 'wxid_owner', accept: m => m.senderRef === 'wxid_owner' });
  for (const id of ['1.0', '1.1', '1.2', '2.0']) bridge.media.set(id, [[200, Buffer.alloc(16)]]);
  // A stranger (owner_ref unset: the shared ring, with media_ids) sends three files; the owner a photo.
  bridge.events.push(mediaEvent(1, ['1.0', '1.1', '1.2'].map(media_id => ({ media_id, kind: 'file', size: 25 * 1048576 })),
    { sender_ref: 'wxid_stranger' }), mediaEvent(2, [{ media_id: '2.0', kind: 'image' }]));
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet, config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    await until(() => channels.received.length === 2);
    assert.deepEqual(channels.stored.map(m => `${m.messageRef}.${m.index}`), ['m1.0', 'm2.0'], 'the rest of the stranger\'s were not asked');
    assert.deepEqual(mediaFetches(bridge), ['2.0'], 'not one of the stranger\'s bytes was fetched');
    assert.equal('attachments' in channels.received[0], false);
    assert.deepEqual(channels.received[1].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }], 'the owner\'s photo after it is taken');
    assertClosedSet(channels.received);
  } finally { await channel.stop(); await bridge.close(); }
});

test('media: sendFile streams the host\'s file to POST /send-file (to the owner or into a group), a caption follows; mediaKinds', async () => {
  const bridge = await fakeBridge();
  const channels = fakeChannels();
  const file = join(dir, 'voice-ab12.wav');
  writeFileSync(file, Buffer.from('RIFF....WAVEfmt '));
  const warnings = [];
  const channel = startWechatChannel({ ownerChannels: channels, logger: { warn: line => warnings.push(line) },
    config: { tokenFile, url: bridge.url }, waitSeconds: 1 });
  try {
    assert.deepEqual(channels.registered.mediaKinds, ['image', 'voice', 'file']);
    bridge.sendFileAnswer = [200, { ref: 'f-9', sent_as: 'file' }];
    assert.deepEqual(await channels.registered.sendFile({ to: 'wxid_owner', replyToRef: 'w1', kind: 'voice', mime: 'audio/wav',
      name: 'voice-ab12.wav', path: file, caption: '听这段' }), { ref: 'f-9' });
    assert.deepEqual(bridge.files[0].query, { to: 'wxid_owner', kind: 'voice', mime: 'audio/wav', name: 'voice-ab12.wav' });
    assert.equal(bridge.files[0].bytes.toString(), 'RIFF....WAVEfmt ');
    assert.equal(bridge.files[0].type, 'application/octet-stream');
    assert.deepEqual(bridge.sent, [{ to: 'wxid_owner', text: '听这段' }], 'the caption after the file');
    assert.ok(warnings.some(line => line.includes('went out as a file')));
    bridge.sendFileAnswer = [200, { ref: null, sent_as: 'image' }];
    assert.deepEqual(await channels.registered.sendFile({ groupRef: 'r1@chatroom', kind: 'image', mime: 'image/png', name: 'i.png', path: file }), {});
    assert.deepEqual(bridge.files[1].query, { room: 'r1@chatroom', kind: 'image', mime: 'image/png', name: 'i.png' });
    assert.equal(bridge.sent.length, 1, 'no caption, no text');
    bridge.sendFileAnswer = [409, { error: 'recipient_unknown' }];
    await assert.rejects(channels.registered.sendFile({ to: 'wxid_owner', kind: 'file', mime: 'application/pdf', name: 'a.pdf', path: file }),
      error => error.code === 'recipient_unknown');
    await assert.rejects(channels.registered.sendFile({ kind: 'file', mime: 'x', name: 'a', path: file }), error => error.code === 'recipient_unknown');
    const big = join(dir, 'big.bin');
    writeFileSync(big, '');
    truncateSync(big, 25 * 1048576 + 1);
    const before = bridge.files.length;
    await assert.rejects(channels.registered.sendFile({ to: 'wxid_owner', kind: 'file', mime: 'x', name: 'big.bin', path: big }),
      error => error.code === 'too_large');
    assert.equal(bridge.files.length, before, 'over 25 MiB never reaches the bridge');
  } finally { await channel.stop(); await bridge.close(); }
});

test('media end to end with the real bridge module: an owner\'s photo stored, a stranger\'s files never fetched, a refused voice bar sent as a file', async () => {
  const types = { Message: { Attachment: 1, Audio: 2, Image: 6, Text: 7 }, Contact: { Official: 2 }, ScanStatus: { Waiting: 2 } };
  const bot = new EventEmitter();
  const sent = [];
  let fetches = 0;
  bot.start = async () => {};
  bot.stop = async () => {};
  bot.puppet = { messageSendFile: async (to, box) => {
    if (box.name.endsWith('.silk')) throw new Error('voice refused');
    sent.push({ to, name: box.name, bytes: box.bytes });
    return 'out-' + sent.length;
  } };
  bot.Contact = { find: async ({ id }) => ({ id, say: async box => ({ id: await bot.puppet.messageSendFile(id, box) }) }) };
  const bridge = createBridge({ makeBot: () => bot, types, token: TOKEN, tmpDir: mkdtempSync(join(tmpdir(), 'wechat-e2e-')),
    makeFileBox: (bytes, name) => ({ name, bytes: Buffer.from(bytes) }) });
  const address = await bridge.listen(0);
  const channels = mediaChannels({ bound: meta => meta.senderRef === 'wxid_owner', accept: m => m.senderRef === 'wxid_owner' });
  const channel = startWechatChannel({ ownerChannels: channels, logger: quiet,
    config: { tokenFile, url: `http://${address.address}:${address.port}` }, waitSeconds: 1, statusEveryMs: 20 });
  const media = (id, from, type, bytes) => ({ id, self: () => false, type: () => type, text: () => '', date: () => new Date(),
    payload: {}, talker: () => ({ id: from, type: () => 0 }),
    toFileBox: async () => { fetches++; return { name: 'x', toStream: async () => Readable.from([bytes]) }; },
    toImage: () => ({ hd: async () => { fetches++; return { name: 'x.jpg', mediaType: 'image/jpeg', toStream: async () => Readable.from([bytes]) }; } }) });
  try {
    assert.deepEqual(await channels.registered.login(), { requested: true });
    bot.emit('scan', 'https://login.weixin.qq.com/l/x', 2);
    bot.emit('login', { id: 'wxid_bot' });
    await until(() => channels.registered.status().connected);
    for (let i = 0; i < 3; i++) bot.emit('message', media('s' + i, 'wxid_stranger', types.Message.Attachment, Buffer.alloc(8)));
    bot.emit('message', media('photo', 'wxid_owner', types.Message.Image, Buffer.from('PHOTO')));
    await until(() => channels.received.length === 4);
    assert.equal(fetches, 1, 'only the owner\'s photo was fetched');
    assert.deepEqual(channels.received.slice(0, 3).map(m => 'attachments' in m), [false, false, false]);
    assert.deepEqual(channels.received[3].attachments, [{ kind: 'image', ref: 'm_0000000000000001' }]);
    assert.equal(channels.opened[0].bytes.toString(), 'PHOTO');
    assertClosedSet(channels.received);
    const file = join(dir, 'voice-e2e.wav');
    writeFileSync(file, Buffer.from('RIFF-ish audio'));
    assert.deepEqual(await channels.registered.sendFile({ to: 'wxid_owner', kind: 'voice', mime: 'audio/wav', name: 'voice-e2e.wav', path: file }),
      { ref: 'out-1' });
    assert.deepEqual(sent.map(x => [x.to, x.name, x.bytes.toString()]), [['wxid_owner', 'voice-e2e.wav', 'RIFF-ish audio']]);
  } finally { await channel.stop(); await bridge.close(); }
});
