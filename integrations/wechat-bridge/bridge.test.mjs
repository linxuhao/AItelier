/** The bridge's HTTP layer against a fake Wechaty: no puppet is loaded and nothing
 *  reaches WeChat. The fake emits Wechaty-shaped events and answers Contact.find. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createBridge, splitText, bindHost, SEND_CHUNK, HOST, CONTAINER_HOST, LOGIN_LIMITS } from './bridge.mjs';

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

/** `calls` records each start and stop in order; `startError` makes start() reject. */
function fakeBot({ known = ['wxid_owner'] } = {}) {
  const bot = new EventEmitter();
  bot.said = [];
  bot.sayError = undefined;
  bot.calls = [];
  bot.startError = undefined;
  bot.start = async () => { bot.calls.push('start'); if (bot.startError) throw bot.startError; };
  bot.stop = async () => { bot.calls.push('stop'); };
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
    for (const [path, method, body] of [['/status', 'GET'], ['/events?after=0', 'GET'], ['/send', 'POST', { to: 'wxid_owner', text: 'x' }], ['/login', 'POST'], ['/nope', 'GET']]) {
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

test('while a scan is needed, status carries the QR drawn as an SVG, and never logs it', async () => {
  const QR = 'https://login.weixin.qq.com/l/SECRET-LOGIN-TOKEN';
  const drawn = [], logs = [];
  const renderQr = qr => { drawn.push(qr); return `<svg xmlns="http://www.w3.org/2000/svg">${drawn.length}</svg>`; };
  const { bot, bridge, json } = await started({ renderQr, log: line => logs.push(line) });
  try {
    let [, body] = await json('/status');
    assert.equal(body.qr_svg, undefined, 'no QR before a scan is asked for');
    bot.emit('scan', QR, types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.deepEqual([body.qr, body.qr_svg, drawn], [QR, '<svg xmlns="http://www.w3.org/2000/svg">1</svg>', [QR]]);
    bot.emit('scan', '', types.ScanStatus.Scanned);
    [, body] = await json('/status');
    assert.equal(body.qr_svg, undefined, 'a scanned QR is not shown again');
    bot.emit('scan', QR + '-2', types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.equal(body.qr_svg, '<svg xmlns="http://www.w3.org/2000/svg">2</svg>', 'a new QR is drawn anew');
    bot.emit('login', { id: 'wxid_bot' });
    [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.qr_svg], [true, undefined]);
    assert.ok(!logs.some(line => line.includes('SECRET-LOGIN-TOKEN') || line.includes('<svg')), logs.join('\n'));
  } finally { await bridge.close(); }
});

test('a QR that cannot be drawn leaves only the payload, and the failure is logged without it', async () => {
  const logs = [];
  const renderQr = () => { throw new Error('too long: SECRET-LOGIN-TOKEN'); };
  const { bot, bridge, json } = await started({ renderQr, log: line => logs.push(line) });
  try {
    bot.emit('scan', 'https://login.weixin.qq.com/l/SECRET-LOGIN-TOKEN', types.ScanStatus.Waiting);
    const [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.qr_svg], [false, undefined]);
    assert.ok(body.qr, 'the payload itself is still served');
    assert.deepEqual(logs, ['the login QR could not be drawn', 'login QR waiting (GET /status carries it)']);
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

test('a message WeChat does not date carries no time, never the bridge\'s own', async () => {
  const { bot, bridge, json } = await started();
  try {
    bot.emit('message', message({ id: 'undated', date: null, text: '同意 proposal p-1' }));
    bot.emit('message', message({ id: 'bad-date', date: new Date(Number.NaN) }));
    const [, body] = await json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, 'at' in e]), [['undated', false], ['bad-date', false]]);
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

test('the bridge starts idle (boot) and asks WeChat for nothing by itself', async () => {
  const { bot, bridge, json } = await started();
  try {
    const [status, body] = await json('/status');
    assert.equal(status, 200);
    assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.qr, body.qr_svg], ['idle', 'boot', false, undefined, undefined]);
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual(bot.calls, [], 'the puppet is not started without a request');
  } finally { await bridge.close(); }
});

test('POST /login stops then starts the puppet, and the phases follow the QR to a login', async () => {
  const QR = 'https://login.weixin.qq.com/l/SECRET-LOGIN-TOKEN';
  const logs = [];
  const { bot, bridge, json } = await started({ log: line => logs.push(line), renderQr: () => '<svg xmlns="http://www.w3.org/2000/svg"/>' });
  try {
    const answer = await json('/login', { method: 'POST' });
    assert.deepEqual(answer, [202, { phase: 'starting' }]);
    assert.equal((await json('/status'))[1].phase, 'starting');
    await bridge.settled();
    assert.deepEqual(bot.calls, ['stop', 'start'], 'stop first: start() on a puppet still active returns at once');
    bot.emit('scan', QR, types.ScanStatus.Waiting);
    let [, body] = await json('/status');
    assert.deepEqual([body.phase, body.idle_reason, body.qr], ['waiting_qr', undefined, QR]);
    assert.ok(body.qr_svg);
    bot.emit('scan', '', types.ScanStatus.Scanned);
    [, body] = await json('/status');
    assert.deepEqual([body.phase, body.qr, body.qr_svg], ['scanned', undefined, undefined]);
    bot.emit('login', { id: 'wxid_bot' });
    [, body] = await json('/status');
    assert.deepEqual([body.phase, body.logged_in, body.user_id], ['logged_in', true, 'wxid_bot']);
    assert.ok(!logs.some(line => line.includes('SECRET-LOGIN-TOKEN') || line.includes('<svg')), logs.join('\n'));
    assert.ok(!JSON.stringify(answer).includes('SECRET'), 'the answer to /login never carries a QR');
  } finally { await bridge.close(); }
});

test('an expired or cancelled QR, a logout and a failure before login leave the bridge idle: the puppet stopped, never started again, the process up', async () => {
  const cases = [
    ['qr_expired', bot => bot.emit('scan', '', types.ScanStatus.Timeout)],
    ['qr_cancelled', bot => bot.emit('scan', '', types.ScanStatus.Cancel)],
    // neo's native session fails before any QR: no logout, no scan timeout follows.
    ['login_failed', bot => bot.emit('error', new Error('Neo could not start QR login'))],
    ['logged_out', bot => { bot.emit('login', { id: 'wxid_bot' }); bot.emit('logout', { id: 'wxid_bot' }); }],
  ];
  for (const [reason, happen] of cases) {
    const { bot, bridge, json } = await started({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
    try {
      assert.equal((await json('/login', { method: 'POST' }))[0], 202);
      await bridge.settled();
      bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
      happen(bot);
      // neo also reports the ended QR as an error after the scan event: it changes nothing.
      bot.emit('error', new Error('Neo QR login ended: expired'));
      await bridge.settled();
      const [, body] = await json('/status');
      assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.qr, body.qr_svg], ['idle', reason, false, undefined, undefined], reason);
      assert.deepEqual(bot.calls, ['stop', 'start', 'stop'], `${reason}: stopped once, not started again`);
      await new Promise(resolve => setTimeout(resolve, 30));
      assert.deepEqual(bot.calls, ['stop', 'start', 'stop'], `${reason}: no QR asked for by itself`);
      // A new request is taken again.
      assert.equal((await json('/login', { method: 'POST' }))[0], 202, reason);
      await bridge.settled();
      assert.deepEqual(bot.calls.slice(3), ['stop', 'start']);
    } finally { await bridge.close(); }
  }
});

test('an error while logged in is only logged', async () => {
  const logs = [];
  const { bot, bridge, json } = await started({ log: line => logs.push(line) });
  try {
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('error', new Error('message not found for id: m1'));
    assert.equal((await json('/status'))[1].phase, 'logged_in');
    assert.ok(logs.includes('error: message not found for id: m1'));
    assert.deepEqual(bot.calls, []);
  } finally { await bridge.close(); }
});

test('the logout and error events of the bridge\'s own stop do not end the attempt it makes', async () => {
  const { bot, bridge, json } = await started();
  try {
    // Wechaty emits logout from a stop of a logged-in puppet, and may report a stop's failure.
    bot.stop = async () => { bot.calls.push('stop'); bot.emit('logout', {}); bot.emit('error', new Error('stopping')); };
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    assert.deepEqual(bot.calls, ['stop', 'start']);
    assert.equal((await json('/status'))[1].phase, 'starting');
  } finally { await bridge.close(); }
});

test('POST /login refuses while logged in, while a QR waits or is scanned, and while one is starting, changing nothing', async () => {
  const { bot, bridge, json } = await started();
  try {
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'login_starting' }]);
    await bridge.settled();
    bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'qr_waiting' }]);
    bot.emit('scan', '', types.ScanStatus.Scanned);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'qr_waiting' }]);
    bot.emit('login', { id: 'wxid_bot' });
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'already_logged_in' }]);
    await bridge.settled();
    assert.deepEqual(bot.calls, ['stop', 'start'], 'no refusal touched the puppet');
    assert.equal((await json('/status'))[1].phase, 'logged_in');
  } finally { await bridge.close(); }
});

test('two requests at once start the puppet once', async () => {
  const { bot, bridge, json } = await started();
  try {
    const answers = await Promise.all([json('/login', { method: 'POST' }), json('/login', { method: 'POST' })]);
    assert.deepEqual(answers.map(([status]) => status).sort(), [202, 409]);
    await bridge.settled();
    assert.deepEqual(bot.calls, ['stop', 'start']);
  } finally { await bridge.close(); }
});

test('POST /login is rate limited: 2 minutes apart and 5 an hour, with retry_after_s', async () => {
  let clock = Date.parse('2026-11-20T12:00:00Z');
  const { bot, bridge, json } = await started({ now: () => new Date(clock) });
  const expire = async () => { await bridge.settled(); bot.emit('scan', '', types.ScanStatus.Timeout); await bridge.settled(); };
  try {
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await expire();
    clock += 60_000;
    assert.deepEqual(await json('/login', { method: 'POST' }), [429, { refused: 'rate_limited', retry_after_s: 60 }]);
    clock += 59_999;
    assert.deepEqual(await json('/login', { method: 'POST' }), [429, { refused: 'rate_limited', retry_after_s: 1 }]);
    clock += 1;
    assert.equal((await json('/login', { method: 'POST' }))[0], 202, 'exactly 2 minutes later');
    for (let i = 0; i < 3; i++) { await expire(); clock += 120_000; assert.equal((await json('/login', { method: 'POST' }))[0], 202); }
    await expire();
    // Five in the last 8 minutes: the sixth waits until the first is an hour old.
    clock += 120_000;
    assert.deepEqual(await json('/login', { method: 'POST' }), [429, { refused: 'rate_limited', retry_after_s: 3600 - 600 }]);
    clock = Date.parse('2026-11-20T13:00:00Z');
    assert.equal((await json('/login', { method: 'POST' }))[0], 202, 'the first is an hour old');
    const starts = bot.calls.filter(call => call === 'start').length;
    assert.equal(starts, 6, 'a refused request never started the puppet');
  } finally { await bridge.close(); }
});

test('a start that fails, or a QR that never appears, ends the attempt as login_failed', async () => {
  const logs = [];
  const { bot, bridge, json } = await started({ log: line => logs.push(line), loginLimits: { minGapMs: 0, perHour: 5, qrWaitMs: 50 } });
  try {
    bot.startError = new Error('puppet start exploded');
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    await until(() => bridge.state.phase === 'idle');
    assert.deepEqual([bridge.state.phase, bridge.state.idle_reason], ['idle', 'login_failed']);
    assert.ok(logs.includes('starting the puppet failed: puppet start exploded'));
    bot.startError = undefined;
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    assert.equal(bridge.state.phase, 'starting');
    await until(() => bridge.state.phase === 'idle', 2000);
    assert.equal(bridge.state.idle_reason, 'login_failed', 'no QR within qrWaitMs');
    // A QR that comes in time cancels that timer.
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    await new Promise(resolve => setTimeout(resolve, 120));
    assert.equal(bridge.state.phase, 'waiting_qr');
  } finally { await bridge.close(); }
});

test('with login disabled (WECHAT_BRIDGE_SKIP_LOGIN=1) every request is refused and the puppet never starts', async () => {
  const { bot, bridge, json } = await started({ loginEnabled: false });
  try {
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'login_disabled' }]);
    assert.deepEqual(bot.calls, []);
    assert.equal((await json('/status'))[1].phase, 'idle');
  } finally { await bridge.close(); }
});

test('splitText keeps surrogate pairs whole', () => {
  const text = '😀'.repeat(5);
  const parts = splitText(text, 2);
  assert.deepEqual(parts, ['😀😀', '😀😀', '😀']);
});

test('run.sh publishes the container\'s 18731 on the host\'s 127.0.0.1:18741 only, or on WECHAT_BRIDGE_PORT', () => {
  // A stand-in `docker` on PATH prints its arguments: nothing is started.
  const home = mkdtempSync(join(tmpdir(), 'wechat-bridge-run-'));
  mkdirSync(join(home, '.aitelier-secrets'));
  writeFileSync(join(home, '.aitelier-secrets', 'wechat-bridge-token'), 'throwaway\n');
  mkdirSync(join(home, 'bin'));
  writeFileSync(join(home, 'bin', 'docker'), '#!/bin/sh\nprintf "%s\\n" "$@"\n');
  chmodSync(join(home, 'bin', 'docker'), 0o755);
  const script = fileURLToPath(new URL('./run.sh', import.meta.url));
  const run = extra => execFileSync('sh', [script], { encoding: 'utf8',
    env: { PATH: `${join(home, 'bin')}:/usr/bin:/bin`, HOME: home, ...extra } }).split('\n');
  const published = args => args.filter((arg, i) => args[i - 1] === '--publish');
  assert.deepEqual(published(run({})), ['127.0.0.1:18741:18731']);
  assert.deepEqual(published(run({ WECHAT_BRIDGE_PORT: '19999' })), ['127.0.0.1:19999:18731']);
});

test('run.sh puts the container on its own network, creating it only when missing, and keeps every hardening flag', () => {
  // A stand-in `docker` logs each call (one line, its arguments joined by spaces) and
  // answers `network inspect` as if the network existed only when NETWORK_EXISTS=1.
  const home = mkdtempSync(join(tmpdir(), 'wechat-bridge-net-'));
  mkdirSync(join(home, '.aitelier-secrets'));
  writeFileSync(join(home, '.aitelier-secrets', 'wechat-bridge-token'), 'throwaway\n');
  mkdirSync(join(home, 'bin'));
  const log = join(home, 'calls.log');
  writeFileSync(join(home, 'bin', 'docker'), '#!/bin/sh\necho "$*" >> "$CALLS"\n'
    + 'if [ "$1 $2" = "network inspect" ] && [ "$NETWORK_EXISTS" != 1 ]; then exit 1; fi\n');
  chmodSync(join(home, 'bin', 'docker'), 0o755);
  const script = fileURLToPath(new URL('./run.sh', import.meta.url));
  const calls = exists => {
    writeFileSync(log, '');
    execFileSync('sh', [script], { encoding: 'utf8', env: { PATH: `${join(home, 'bin')}:/usr/bin:/bin`, HOME: home,
      CALLS: log, NETWORK_EXISTS: exists ? '1' : '0' } });
    return readFileSync(log, 'utf8').trim().split('\n');
  };
  const missing = calls(false);
  assert.deepEqual(missing.slice(0, 2), ['network inspect aitelier-wechat-bridge', 'network create aitelier-wechat-bridge']);
  assert.equal(missing.length, 3);
  const run = missing[2].split(' ');
  assert.equal(run[0], 'run');
  assert.equal(run[run.indexOf('--network') + 1], 'aitelier-wechat-bridge');
  for (const flag of ['--read-only', '--cap-drop', '--security-opt', '--memory', '--memory-swap', '--cpus', '--pids-limit', '--tmpfs']) {
    assert.ok(run.includes(flag), flag);
  }
  assert.equal(run[run.indexOf('--publish') + 1], '127.0.0.1:18741:18731');
  // A restart starts idle and asks WeChat for nothing, so Docker may always restart it.
  assert.equal(run[run.indexOf('--restart') + 1], 'unless-stopped');
  // An existing network is used as it is: no second create.
  const present = calls(true);
  assert.deepEqual(present.map(line => line.split(' ').slice(0, 2).join(' ')), ['network inspect', 'run --detach']);
  assert.ok(present[1].includes('--network aitelier-wechat-bridge'));
});
