/** The bridge's HTTP layer against a fake Wechaty: no puppet is loaded and nothing
 *  reaches WeChat. The fake emits Wechaty-shaped events and answers Contact.find. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createBridge, splitText, superviseLogin, bindHost, SEND_CHUNK, HOST, CONTAINER_HOST } from './bridge.mjs';

// The values of Wechaty's own enums (wechaty-puppet types) that the bridge reads.
const types = { Message: { Text: 7, Image: 6 }, Contact: { Unknown: 0, Individual: 1, Official: 2 },
  ScanStatus: { Cancel: 1, Waiting: 2, Scanned: 3, Confirmed: 4, Timeout: 5 } };
const TOKEN = 'test-token-123';

const until = async (check, ms = 3000) => {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error('timed out waiting');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
};

function fakeBot({ known = ['wxid_owner'] } = {}) {
  const bot = new EventEmitter();
  bot.said = [];
  bot.sayError = undefined;
  bot.Contact = {
    find: async ({ id }) => known.includes(id) ? {
      id,
      say: async text => {
        if (bot.sayError) throw bot.sayError;
        bot.said.push({ to: id, text });
        return { id: 'out-' + bot.said.length };
      },
    } : undefined,
  };
  return bot;
}

function message({ id = 'm1', from = 'wxid_owner', text = 'hello', type = types.Message.Text, self = false,
  room = undefined, contactType = types.Contact.Unknown, date = new Date('2026-11-18T10:00:00Z') } = {}) {
  return { id, self: () => self, room: () => room, type: () => type, text: () => text, date: () => date,
    talker: () => ({ id: from, type: () => contactType }) };
}

async function started(options = {}) {
  const bot = fakeBot(options);
  const bridge = createBridge({ bot, types, token: TOKEN, ...options });
  const address = await bridge.listen(0);
  const base = `http://${address.address}:${address.port}`;
  const call = (path, { token = TOKEN, method = 'GET', body } = {}) => fetch(base + path, {
    method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) });
  return { bot, bridge, address, call, json: async (...args) => { const r = await call(...args); return [r.status, await r.json()]; } };
}

test('listens on 127.0.0.1 only', async () => {
  const { bridge, address } = await started();
  try {
    assert.equal(HOST, '127.0.0.1');
    assert.equal(address.address, '127.0.0.1');
  } finally { await bridge.close(); }
});

test('the bind address is loopback unless the bridge runs in its container', async () => {
  assert.equal(bindHost(undefined, false), '127.0.0.1');
  assert.equal(bindHost('', true), '127.0.0.1');
  assert.equal(bindHost('127.0.0.1', true), '127.0.0.1');
  assert.equal(bindHost('0.0.0.0', true), '0.0.0.0', 'the container interface, published on the host loopback only');
  assert.throws(() => bindHost('0.0.0.0', false), /refused: bind address/, 'never every interface outside a container');
  for (const other of ['::', '192.168.1.5', 'localhost', '0.0.0.0 ']) assert.throws(() => bindHost(other, true), /refused/);
  // The listener takes that address.
  const bridge = createBridge({ bot: fakeBot(), types, token: TOKEN });
  try {
    const address = await bridge.listen(0, bindHost(CONTAINER_HOST, true));
    assert.equal(address.address, '0.0.0.0');
    const r = await fetch(`http://127.0.0.1:${address.port}/status`, { headers: { authorization: `Bearer ${TOKEN}` } });
    assert.equal(r.status, 200);
  } finally { await bridge.close(); }
});

test('refuses to start without a token', () => {
  assert.throws(() => createBridge({ bot: fakeBot(), types, token: '  \n' }), /no token/);
  assert.throws(() => createBridge({ bot: fakeBot(), types, token: undefined }), /no token/);
});

test('every endpoint needs the bearer token', async () => {
  const { bridge, json } = await started();
  try {
    for (const [path, method, body] of [['/status', 'GET'], ['/events?after=0', 'GET'], ['/send', 'POST', { to: 'wxid_owner', text: 'x' }], ['/nope', 'GET']]) {
      assert.deepEqual(await json(path, { method, body, token: null }), [401, { error: 'unauthorized' }], path + ' without a token');
      assert.deepEqual(await json(path, { method, body, token: 'wrong' }), [401, { error: 'unauthorized' }], path + ' with a wrong token');
    }
    const r = await fetch(`http://${HOST}:${bridge.server.address().port}/status`, { headers: { authorization: TOKEN } });
    assert.equal(r.status, 401, 'a token without the Bearer scheme is refused');
    assert.equal((await json('/status'))[0], 200);
  } finally { await bridge.close(); }
});

test('status follows scan, login and logout, and carries the QR only while it can be scanned', async () => {
  const { bot, bridge, json } = await started();
  try {
    let [, body] = await json('/status');
    assert.equal(body.logged_in, false);
    assert.equal(body.qr, undefined);
    assert.ok(body.since);
    bot.emit('scan', 'https://login.weixin.qq.com/l/abc', types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.qr], [false, 'https://login.weixin.qq.com/l/abc']);
    bot.emit('scan', '', types.ScanStatus.Scanned);
    [, body] = await json('/status');
    assert.equal(body.qr, undefined, 'a scanned QR is not offered again');
    bot.emit('login', { id: 'wxid_bot' });
    [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.user_id, body.qr], [true, 'wxid_bot', undefined]);
    bot.emit('logout', { id: 'wxid_bot' });
    [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.user_id], [false, undefined]);
  } finally { await bridge.close(); }
});

test('only 1:1 text messages from another contact become events', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('message', message({ id: 'room', room: { id: 'r@chatroom' } }));
    bot.emit('message', message({ id: 'self', self: true }));
    bot.emit('message', message({ id: 'image', type: types.Message.Image }));
    bot.emit('message', message({ id: 'official-type', from: 'wxid_x', contactType: types.Contact.Official }));
    bot.emit('message', message({ id: 'official-id', from: 'gh_abcdef' }));
    bot.emit('message', message({ id: 'owner', text: '同意 proposal p-1' }));
    const [status, body] = await json('/events?after=0');
    assert.equal(status, 200);
    assert.equal(body.events.length, 1);
    const [event] = body.events;
    assert.deepEqual({ ...event, seq: undefined }, { seq: undefined, type: 'message', sender_ref: 'wxid_owner',
      message_ref: 'owner', text: '同意 proposal p-1', at: '2026-11-18T10:00:00.000Z' });
    assert.equal(body.next_after, event.seq);
  } finally { await bridge.close(); }
});

test('a long poll returns as soon as a message arrives, and empty with the same cursor when none does', async () => {
  const { bot, bridge, json } = await started();
  try {
    let [, body] = await json('/events?after=0&wait=0');
    assert.deepEqual(body, { events: [], next_after: 0 });
    const t0 = Date.now();
    [, body] = await json('/events?after=0&wait=1');
    assert.deepEqual(body, { events: [], next_after: 0 });
    assert.ok(Date.now() - t0 >= 900, 'the empty answer waited');
    const polled = json('/events?after=0&wait=20');
    await new Promise(resolve => setTimeout(resolve, 100));
    const t1 = Date.now();
    bot.emit('message', message({ id: 'late' }));
    [, body] = await polled;
    assert.ok(Date.now() - t1 < 2000, 'answered on arrival, not at the timeout');
    assert.deepEqual(body.events.map(e => e.message_ref), ['late']);
    const cursor = body.next_after;
    bot.emit('message', message({ id: 'next' }));
    [, body] = await json(`/events?after=${cursor}`);
    assert.deepEqual(body.events.map(e => e.message_ref), ['next'], 'the cursor skips what was already served');
    assert.deepEqual((await json('/events?after=x'))[0], 400);
  } finally { await bridge.close(); }
});

test('a long poll the client abandons releases its waiter and timer', async () => {
  const { bridge, call } = await started();
  try {
    const abort = new AbortController();
    const polled = fetch(`http://${HOST}:${bridge.server.address().port}/events?after=0&wait=20`,
      { headers: { authorization: `Bearer ${TOKEN}` }, signal: abort.signal }).catch(() => 'aborted');
    await until(() => bridge.waiting === 1);
    abort.abort();
    assert.equal(await polled, 'aborted');
    await until(() => bridge.waiting === 0, 2000);
    assert.equal((await call('/status')).status, 200);
  } finally { await bridge.close(); }
});

test('a long poll never waits longer than the cap', async () => {
  const { bridge, json } = await started({ maxWaitSeconds: 1 });
  try {
    const t0 = Date.now();
    const [, body] = await json('/events?after=0&wait=600');
    assert.deepEqual(body, { events: [], next_after: 0 });
    assert.ok(Date.now() - t0 < 3000, 'capped, not 600 s');
  } finally { await bridge.close(); }
});

test('events are kept in a bounded ring', async () => {
  const { bot, bridge, json } = await started({ ringSize: 3 });
  try {
    for (let i = 1; i <= 5; i++) bot.emit('message', message({ id: 'm' + i }));
    const [, body] = await json('/events?after=0');
    assert.deepEqual(body.events.map(e => e.message_ref), ['m3', 'm4', 'm5']);
  } finally { await bridge.close(); }
});

test('a cursor kept from before a bridge restart is below every new sequence number', async () => {
  const first = createBridge({ bot: fakeBot(), types, token: TOKEN, now: () => new Date('2026-11-18T10:00:00Z') });
  const bot = fakeBot();
  const second = createBridge({ bot, types, token: TOKEN, now: () => new Date('2026-11-18T10:05:00Z') });
  const address = await second.listen(0);
  try {
    // Many events in the first life still leave its cursor below the second life's.
    const oldCursor = new Date('2026-11-18T10:00:00Z').getTime() + 10_000;
    bot.emit('message', message({ id: 'after-restart' }));
    const body = await (await fetch(`http://${address.address}:${address.port}/events?after=${oldCursor}`,
      { headers: { authorization: `Bearer ${TOKEN}` } })).json();
    assert.deepEqual(body.events.map(e => e.message_ref), ['after-restart']);
  } finally { await second.close(); first.server.close(); }
});

test('send refuses before login and to a peer this login has not seen', async () => {
  const { bot, bridge, json } = await started();
  try {
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'hi' } }), [409, { error: 'not_logged_in' }]);
    bot.emit('login', { id: 'wxid_bot' });
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_stranger', text: 'hi' } }), [409, { error: 'recipient_unknown' }]);
    assert.deepEqual(bot.said, []);
  } finally { await bridge.close(); }
});

test('send writes the text to the peer, splitting long text into parts of at most 1500 characters', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('login', { id: 'wxid_bot' });
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'short' } }), [200, { ref: 'out-1' }]);
    const long = '好'.repeat(SEND_CHUNK) + 'x'.repeat(SEND_CHUNK) + 'tail';
    const [status, body] = await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: long } });
    assert.equal(status, 200);
    assert.equal(body.ref, 'out-2', 'the reference is the first part');
    const parts = bot.said.slice(1).map(s => s.text);
    assert.deepEqual(parts.map(p => Array.from(p).length), [1500, 1500, 4]);
    assert.equal(parts.join(''), long);
  } finally { await bridge.close(); }
});

test('send answers a bad body with 400 and a failed send with send_failed', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('login', { id: 'wxid_bot' });
    assert.deepEqual(await json('/send', { method: 'POST', body: '{not json' }), [400, { error: 'bad_request' }]);
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: '   ' } }), [400, { error: 'bad_request' }]);
    assert.deepEqual(await json('/send', { method: 'POST', body: { text: 'hi' } }), [400, { error: 'bad_request' }]);
    bot.sayError = new Error('WeChat rejected the text send');
    const [status, body] = await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'hi' } });
    assert.equal(status, 502);
    assert.equal(body.error, 'send_failed');
  } finally { await bridge.close(); }
});

test('a body over 64 KiB is refused with 413 and nothing is sent', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('login', { id: 'wxid_bot' });
    const big = { to: 'wxid_owner', text: 'x'.repeat(64 * 1024) };
    assert.deepEqual(await json('/send', { method: 'POST', body: big }), [413, { error: 'body_too_large' }]);
    assert.deepEqual(bot.said, []);
    const fits = { to: 'wxid_owner', text: 'x'.repeat(60 * 1024) };
    assert.equal((await json('/send', { method: 'POST', body: fits }))[0], 200);
  } finally { await bridge.close(); }
});

test('a peer neo no longer knows is recipient_unknown, not a failed send', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('login', { id: 'wxid_bot' });
    bot.sayError = new Error('Neo supports text to an observed peer or group');
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'hi' } }), [409, { error: 'recipient_unknown' }]);
  } finally { await bridge.close(); }
});

test('superviseLogin ends the process when the login cannot go on, and only then', () => {
  const bot = new EventEmitter();
  const stops = [];
  const lines = [];
  superviseLogin({ bot, types, stop: (reason, code) => stops.push([reason, code]), log: line => lines.push(line) });
  bot.emit('scan', 'qr', types.ScanStatus.Waiting);
  bot.emit('scan', '', types.ScanStatus.Scanned);
  assert.deepEqual(stops, []);
  // neo's native session fails before any QR: no logout, no scan timeout follows.
  bot.emit('error', new Error('Neo session failed'));
  assert.deepEqual(stops, [['the puppet failed before a login', 75]]);

  stops.length = 0;
  const loggedIn = new EventEmitter();
  superviseLogin({ bot: loggedIn, types, stop: (reason, code) => stops.push([reason, code]) });
  loggedIn.emit('login', { id: 'wxid_bot' });
  loggedIn.emit('error', new Error('message not found for id: m1'));
  assert.deepEqual(stops, [], 'an error while logged in is only logged');
  loggedIn.emit('logout', { id: 'wxid_bot' });
  assert.deepEqual(stops, [['logged out', 75]]);

  for (const status of [types.ScanStatus.Timeout, types.ScanStatus.Cancel]) {
    const scanning = new EventEmitter();
    stops.length = 0;
    superviseLogin({ bot: scanning, types, stop: (reason, code) => stops.push([reason, code]) });
    scanning.emit('scan', '', status);
    assert.deepEqual(stops, [['login QR ended without a login', 75]]);
  }
  assert.ok(lines.some(line => line.startsWith('error: Neo session failed')));
});

test('splitText keeps surrogate pairs whole', () => {
  const text = '😀'.repeat(5);
  const parts = splitText(text, 2);
  assert.deepEqual(parts, ['😀😀', '😀😀', '😀']);
});
