/** The bridge's HTTP layer against fake Wechaty bots: no puppet is loaded and nothing
 *  reaches WeChat. Each fake emits Wechaty-shaped events and answers Contact.find; the
 *  bridge builds one per POST /login (`makeBot`). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, mkdirSync, chmodSync, statSync, symlinkSync, existsSync } from 'node:fs';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createBridge, splitText, bindHost, SEND_CHUNK, HOST, CONTAINER_HOST, LOGIN_LIMITS, MEDIA_LIMITS, MEDIA_FETCH, MEDIA_SEND, mediaOf,
  sendPlan, voicePcm, isBubbleJpeg, inboundVoiceMs, wavByteCap, isGoneError, RESUME_LIMITS, accountRecord, ACCOUNT_FILE } from './bridge.mjs';

// The values of Wechaty's own enums (wechaty-puppet types) that the bridge reads.
const types = { Message: { Unknown: 0, Attachment: 1, Audio: 2, Emoticon: 5, Image: 6, Text: 7, Video: 15 }, Contact: { Unknown: 0, Individual: 1, Official: 2 },
  ScanStatus: { Cancel: 1, Waiting: 2, Scanned: 3, Confirmed: 4, Timeout: 5 } };
const TOKEN = 'test-token-123';
const HOUR = 3_600_000;

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
  // Step 24.2: a FileBox said to a contact or a room goes to the puppet's messageSendFile, as
  // Wechaty's say() does; `refuse(name)` true makes the fork refuse that FileBox (the error it
  // throws, `refusal`, is the fork's own rejection unless a test sets another).
  bot.files = [];
  bot.refuse = () => false;
  bot.refusal = 'WeChat rejected the media send';
  // The fork's live session: encodeVoice(pcmBase64) -> {voice (base64 SILK), durationMs}, as
  // the delivered addon answers (16 kHz mono 16-bit: 32 bytes a millisecond). `encodeError`
  // makes it refuse; `encoded` records the PCM it was given.
  bot.encoded = [];
  bot.encodeError = undefined;
  bot.puppet = {
    session: {
      encodeVoice: async pcm => {
        bot.encoded.push(pcm);
        if (bot.encodeError) throw bot.encodeError;
        return { voice: Buffer.from('SILK:' + pcm).toString('base64'), durationMs: Buffer.from(pcm, 'base64').length / 32 };
      },
    },
    messageSendFile: async (to, box) => {
      if (bot.refuse(box.name)) throw new Error(bot.refusal);
      bot.files.push({ to, name: box.name, bytes: box.bytes });
      return 'file-out-' + bot.files.length;
    },
  };
  bot.Contact = {
    find: async ({ id }) => known.includes(id) ? {
      id,
      say: async text => {
        if (bot.sayError) throw bot.sayError;
        if (typeof text !== 'string') return { id: await bot.puppet.messageSendFile(id, text) };
        bot.said.push({ to: id, text });
        return { id: 'out-' + bot.said.length };
      },
    } : undefined,
  };
  return bot;
}

/** A message shaped like the live puppet's: neo's payload carries `roomId` for a group
 *  message and nothing for a 1:1 one, and `room()` throws the TypeError Wechaty raises
 *  when the puppet answers no room payload (`this.roomRawPayload is not a function`) - a
 *  throwing fake here is the witness that the bridge never asks for a Room object. */
function message({ id = 'm1', from = 'wxid_owner', text = 'hello', type = types.Message.Text, self = false,
  roomId, contactType = types.Contact.Unknown, date = new Date('2026-11-18T10:00:00Z') } = {}) {
  return { id, self: () => self, room: () => { throw new TypeError('this.roomRawPayload is not a function'); },
    type: () => type, text: () => text, date: () => date, payload: roomId ? { roomId } : {},
    talker: () => ({ id: from, type: () => contactType }) };
}

const noBot = () => { throw new Error('no bot expected'); };

/** A listening bridge whose `makeBot` hands out fake bots: `bots` in the order built,
 *  `bot()` the newest. `login()` requests a QR and logs in with it on the new bot. */
async function started(options = {}) {
  const bots = [];
  // `options` is what the bridge built the bot with (step 19.1: {resume: true} for a resume).
  const makeBot = opts => { const made = fakeBot(options); made.options = opts; if (options.prepare) options.prepare(made, opts); bots.push(made); return made; };
  const bridge = createBridge({ makeBot, types, token: TOKEN, ...options });
  const address = await bridge.listen(0);
  const base = `http://${address.address}:${address.port}`;
  const call = (path, { token = TOKEN, method = 'GET', body } = {}) => fetch(base + path, {
    method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) });
  const json = async (...args) => { const r = await call(...args); return [r.status, await r.json()]; };
  const bot = () => bots.at(-1);
  const request = async () => { const answer = await json('/login', { method: 'POST' }); await bridge.settled(); return answer; };
  const login = async () => {
    assert.equal((await request())[0], 202);
    bot().emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    bot().emit('login', { id: 'wxid_bot' });
    assert.equal(bridge.state.phase, 'logged_in');
    return bot();
  };
  return { bots, bot, bridge, address, call, json, request, login };
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
  const bridge = createBridge({ makeBot: noBot, types, token: TOKEN });
  try {
    const address = await bridge.listen(0, bindHost(CONTAINER_HOST, true));
    assert.equal(address.address, '0.0.0.0');
    const r = await fetch(`http://127.0.0.1:${address.port}/status`, { headers: { authorization: `Bearer ${TOKEN}` } });
    assert.equal(r.status, 200);
  } finally { await bridge.close(); }
});

test('refuses to start without a token or a bot factory', () => {
  assert.throws(() => createBridge({ makeBot: noBot, types, token: '  \n' }), /no token/);
  assert.throws(() => createBridge({ makeBot: noBot, types, token: undefined }), /no token/);
  assert.throws(() => createBridge({ types, token: TOKEN }), /no makeBot/);
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
  const { bot: newest, bridge, json, request } = await started();
  try {
    let [, body] = await json('/status');
    assert.equal(body.logged_in, false);
    assert.equal(body.qr, undefined);
    assert.ok(body.since);
    await request();
    const bot = newest();
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
  const { bot: newest, bridge, json, request } = await started({ renderQr, log: line => logs.push(line) });
  try {
    let [, body] = await json('/status');
    assert.equal(body.qr_svg, undefined, 'no QR before a scan is asked for');
    await request();
    const bot = newest();
    bot.emit('scan', QR, types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.deepEqual([body.qr, body.qr_svg, drawn], [QR, '<svg xmlns="http://www.w3.org/2000/svg">1</svg>', [QR]]);
    bot.emit('scan', QR + '-2', types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.equal(body.qr_svg, '<svg xmlns="http://www.w3.org/2000/svg">2</svg>', 'a new QR is drawn anew');
    bot.emit('scan', '', types.ScanStatus.Scanned);
    [, body] = await json('/status');
    assert.equal(body.qr_svg, undefined, 'a scanned QR is not shown again');
    bot.emit('scan', QR + '-3', types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.deepEqual([body.phase, body.qr_svg], ['scanned', undefined], 'once scanned, no QR comes back');
    bot.emit('login', { id: 'wxid_bot' });
    [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.qr_svg], [true, undefined]);
    assert.ok(!logs.some(line => line.includes('SECRET-LOGIN-TOKEN') || line.includes('<svg')), logs.join('\n'));
  } finally { await bridge.close(); }
});

test('a QR that cannot be drawn leaves only the payload, and the failure is logged without it', async () => {
  const logs = [];
  const renderQr = () => { throw new Error('too long: SECRET-LOGIN-TOKEN'); };
  const { bot, bridge, json, request } = await started({ renderQr, log: line => logs.push(line) });
  try {
    await request();
    bot().emit('scan', 'https://login.weixin.qq.com/l/SECRET-LOGIN-TOKEN', types.ScanStatus.Waiting);
    const [, body] = await json('/status');
    assert.deepEqual([body.logged_in, body.qr_svg], [false, undefined]);
    assert.ok(body.qr, 'the payload itself is still served');
    assert.deepEqual(logs, ['login QR requested', 'the login QR could not be drawn', 'login QR waiting (GET /status carries it)']);
  } finally { await bridge.close(); }
});

test('only 1:1 text messages from another contact become events', async () => {
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
    bot.emit('message', message({ id: 'room', roomId: 'r@chatroom' }));
    bot.emit('message', message({ id: 'self', self: true }));
    bot.emit('message', message({ id: 'video', type: types.Message.Video }));
    bot.emit('message', message({ id: 'sticker', type: types.Message.Emoticon }));
    bot.emit('message', message({ id: 'app', type: types.Message.Unknown }));
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
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
    bot.emit('message', message({ id: 'undated', date: null, text: '同意 proposal p-1' }));
    bot.emit('message', message({ id: 'bad-date', date: new Date(Number.NaN) }));
    const [, body] = await json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, 'at' in e]), [['undated', false], ['bad-date', false]]);
  } finally { await bridge.close(); }
});

test('a long poll returns as soon as a message arrives, and empty with the same cursor when none does', async () => {
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
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
  const { bridge, json, login } = await started({ ringSize: 3 });
  try {
    const bot = await login();
    for (let i = 1; i <= 5; i++) bot.emit('message', message({ id: 'm' + i }));
    const [, body] = await json('/events?after=0');
    assert.deepEqual(body.events.map(e => e.message_ref), ['m3', 'm4', 'm5']);
  } finally { await bridge.close(); }
});

test('a cursor kept from before a bridge restart is below every new sequence number', async () => {
  const first = createBridge({ makeBot: noBot, types, token: TOKEN, now: () => new Date('2026-11-18T10:00:00Z') });
  const bot = fakeBot();
  const second = createBridge({ makeBot: () => bot, types, token: TOKEN, now: () => new Date('2026-11-18T10:05:00Z') });
  const address = await second.listen(0);
  try {
    // Many events in the first life still leave its cursor below the second life's.
    const oldCursor = new Date('2026-11-18T10:00:00Z').getTime() + 10_000;
    await fetch(`http://${address.address}:${address.port}/login`, { method: 'POST', headers: { authorization: `Bearer ${TOKEN}` } });
    await second.settled();
    bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('message', message({ id: 'after-restart' }));
    const body = await (await fetch(`http://${address.address}:${address.port}/events?after=${oldCursor}`,
      { headers: { authorization: `Bearer ${TOKEN}` } })).json();
    assert.deepEqual(body.events.map(e => e.message_ref), ['after-restart']);
  } finally { await second.close(); first.server.close(); }
});

test('send refuses before login and to a peer this login has not seen', async () => {
  const { bridge, json, login } = await started();
  try {
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'hi' } }), [409, { error: 'not_logged_in' }]);
    const bot = await login();
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_stranger', text: 'hi' } }), [409, { error: 'recipient_unknown' }]);
    assert.deepEqual(bot.said, []);
  } finally { await bridge.close(); }
});

test('send writes the text to the peer, splitting long text into parts of at most 1500 characters', async () => {
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
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
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
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
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
    const big = { to: 'wxid_owner', text: 'x'.repeat(64 * 1024) };
    assert.deepEqual(await json('/send', { method: 'POST', body: big }), [413, { error: 'body_too_large' }]);
    assert.deepEqual(bot.said, []);
    const fits = { to: 'wxid_owner', text: 'x'.repeat(60 * 1024) };
    assert.equal((await json('/send', { method: 'POST', body: fits }))[0], 200);
  } finally { await bridge.close(); }
});

test('a peer neo no longer knows is recipient_unknown, not a failed send', async () => {
  const { bridge, json, login } = await started();
  try {
    const bot = await login();
    bot.sayError = new Error('Neo supports text to an observed peer or group');
    assert.deepEqual(await json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'hi' } }), [409, { error: 'recipient_unknown' }]);
  } finally { await bridge.close(); }
});

test('the bridge starts idle (boot) and asks WeChat for nothing by itself', async () => {
  const { bots, bridge, json } = await started();
  try {
    const [status, body] = await json('/status');
    assert.equal(status, 200);
    assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.qr, body.qr_svg], ['idle', 'boot', false, undefined, undefined]);
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual(bots, [], 'no bot is built without a request');
  } finally { await bridge.close(); }
});

test('POST /login builds and starts a fresh bot, and the phases follow the QR to a login', async () => {
  const QR = 'https://login.weixin.qq.com/l/SECRET-LOGIN-TOKEN';
  const logs = [];
  const { bots, bridge, json } = await started({ log: line => logs.push(line), renderQr: () => '<svg xmlns="http://www.w3.org/2000/svg"/>' });
  try {
    const answer = await json('/login', { method: 'POST' });
    assert.deepEqual(answer, [202, { phase: 'starting' }]);
    assert.equal((await json('/status'))[1].phase, 'starting');
    await bridge.settled();
    assert.equal(bots.length, 1);
    const [bot] = bots;
    assert.deepEqual(bot.calls, ['start']);
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

test('an expired or cancelled QR, a logout and a failure before login leave the bridge idle: the bot stopped, none started again, the process up', async () => {
  const cases = [
    ['qr_expired', bot => bot.emit('scan', '', types.ScanStatus.Timeout)],
    ['qr_cancelled', bot => bot.emit('scan', '', types.ScanStatus.Cancel)],
    // neo's native session fails before any QR: no logout, no scan timeout follows.
    ['login_failed', bot => bot.emit('error', new Error('Neo could not start QR login'))],
    ['logged_out', bot => { bot.emit('login', { id: 'wxid_bot' }); bot.emit('logout', { id: 'wxid_bot' }); }],
  ];
  for (const [reason, happen] of cases) {
    const { bots, bridge, json, request } = await started({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
    try {
      assert.equal((await request())[0], 202);
      const [bot] = bots;
      bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
      happen(bot);
      // neo also reports the ended QR as an error after the scan event: it changes nothing.
      bot.emit('error', new Error('Neo QR login ended: expired'));
      await bridge.settled();
      const [, body] = await json('/status');
      assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.qr, body.qr_svg], ['idle', reason, false, undefined, undefined], reason);
      assert.deepEqual(bot.calls, ['start', 'stop'], `${reason}: stopped once, not started again`);
      await new Promise(resolve => setTimeout(resolve, 30));
      assert.deepEqual([bots.length, bot.calls], [1, ['start', 'stop']], `${reason}: no QR asked for by itself`);
      // A new request is taken again, on a new bot.
      assert.equal((await request())[0], 202, reason);
      assert.equal(bots.length, 2);
      assert.deepEqual([bots[0].calls, bots[1].calls], [['start', 'stop'], ['start']]);
    } finally { await bridge.close(); }
  }
});

test('an error while logged in is only logged', async () => {
  const logs = [];
  const { bridge, json, login } = await started({ log: line => logs.push(line) });
  try {
    const bot = await login();
    bot.emit('error', new Error('message not found for id: m1'));
    assert.equal((await json('/status'))[1].phase, 'logged_in');
    assert.ok(logs.includes('error: message not found for id: m1'));
    assert.deepEqual(bot.calls, ['start']);
  } finally { await bridge.close(); }
});

test('whatever an ended attempt\'s bot emits later changes nothing: its stop\'s logout and error, a late QR or login', async () => {
  const { bots, bridge, json, request } = await started({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 },
    // Wechaty emits logout from a stop of a logged-in puppet, and may report a stop's failure.
    prepare: made => { made.stop = async () => { made.calls.push('stop'); made.emit('logout', {}); made.emit('error', new Error('stopping')); }; } });
  try {
    await request();
    const [first] = bots;
    first.emit('scan', '', types.ScanStatus.Timeout);
    await bridge.settled();
    assert.deepEqual([bridge.state.phase, bridge.state.idle_reason, first.calls], ['idle', 'qr_expired', ['start', 'stop']]);
    // A QR or a login that the ended attempt's puppet still delivers (security review case A, B).
    first.emit('scan', 'https://login.weixin.qq.com/l/late', types.ScanStatus.Waiting);
    first.emit('scan', '', types.ScanStatus.Scanned);
    first.emit('login', { id: 'wxid_late' });
    first.emit('message', message({ id: 'late' }));
    let [, body] = await json('/status');
    assert.deepEqual([body.phase, body.idle_reason, body.qr, body.logged_in], ['idle', 'qr_expired', undefined, false]);
    assert.deepEqual((await json('/events?after=0'))[1].events, []);
    // The next request is taken, and its own bot's stop events do not end it either.
    await request();
    const second = bots[1];
    first.emit('error', new Error('Timeout after 15000 ms'));
    first.emit('logout', {});
    first.emit('scan', '', types.ScanStatus.Timeout);
    first.emit('login', { id: 'wxid_late' });
    assert.equal(bridge.state.phase, 'starting', 'the old bot\'s events neither end nor complete the new attempt');
    second.emit('scan', 'https://login.weixin.qq.com/l/q2', types.ScanStatus.Waiting);
    [, body] = await json('/status');
    assert.deepEqual([body.phase, body.qr], ['waiting_qr', 'https://login.weixin.qq.com/l/q2']);
  } finally { await bridge.close(); }
});

test('events an attempt\'s own bot sends out of order count only in the phase they can follow', async () => {
  const { bot, bridge, request } = await started();
  try {
    await request();
    bot().emit('scan', '', types.ScanStatus.Scanned);
    assert.equal(bridge.state.phase, 'starting', 'nothing to scan yet');
    bot().emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    bot().emit('login', { id: 'wxid_bot' });
    bot().emit('scan', 'https://login.weixin.qq.com/l/again', types.ScanStatus.Waiting);
    bot().emit('scan', '', types.ScanStatus.Scanned);
    assert.deepEqual([bridge.state.phase, bridge.state.qr], ['logged_in', undefined], 'a login is not undone by a stray scan');
  } finally { await bridge.close(); }
});

test('a QR the puppet never reports over ends at qrLifeMs, so a request is never refused forever', async () => {
  const { bots, bridge, json, request } = await started({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0, qrLifeMs: 80 } });
  try {
    await request();
    bots[0].emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'qr_waiting' }]);
    bots[0].emit('scan', '', types.ScanStatus.Scanned);
    await until(() => bridge.state.phase === 'idle', 2000);
    assert.equal(bridge.state.idle_reason, 'qr_expired');
    await bridge.settled();
    assert.deepEqual(bots[0].calls, ['start', 'stop']);
    assert.equal((await request())[0], 202);
    // A login in time cancels that bound.
    bots[1].emit('scan', 'https://login.weixin.qq.com/l/q2', types.ScanStatus.Waiting);
    bots[1].emit('login', { id: 'wxid_bot' });
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(bridge.state.phase, 'logged_in');
  } finally { await bridge.close(); }
});

test('a slow stop of an ended bot never holds up or ends the next attempt', async () => {
  let release;
  const { bots, bridge, request } = await started({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
  try {
    await request();
    bots[0].stop = () => { bots[0].calls.push('stop'); return new Promise(resolve => { release = resolve; }); };
    bots[0].emit('error', new Error('Neo could not start QR login'));
    assert.equal(bridge.state.phase, 'idle');
    // The first bot is still stopping; the next request starts its own bot at once.
    const [status] = await (async () => { const r = await fetch(`http://${HOST}:${bridge.server.address().port}/login`,
      { method: 'POST', headers: { authorization: `Bearer ${TOKEN}` } }); return [r.status]; })();
    assert.equal(status, 202);
    await until(() => bots[1]?.calls.includes('start'));
    bots[0].emit('error', new Error('Timeout after 15000 ms'));
    assert.equal(bridge.state.phase, 'starting');
    release();
    await bridge.settled();
    assert.deepEqual([bots[0].calls, bots[1].calls], [['start', 'stop'], ['start']]);
  } finally { await bridge.close(); }
});

test('close() stops the current bot, and a start still running when it closes ends nothing later', async () => {
  let finishStart;
  const { bots, bridge, json } = await started({ prepare: made => {
    made.start = () => { made.calls.push('start'); return new Promise(resolve => { finishStart = resolve; }); }; } });
  assert.equal((await json('/login', { method: 'POST' }))[0], 202);
  const closed = bridge.close();
  finishStart();
  await closed;
  await bridge.settled();
  assert.deepEqual(bots[0].calls, ['start', 'stop']);
  bots[0].emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
  assert.equal(bridge.state.qr, undefined);
});

test('POST /login refuses while logged in, while a QR waits or is scanned, and while one is starting, changing nothing', async () => {
  const { bots, bridge, json } = await started();
  try {
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'login_starting' }]);
    await bridge.settled();
    const [bot] = bots;
    bot.emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'qr_waiting' }]);
    bot.emit('scan', '', types.ScanStatus.Scanned);
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'qr_waiting' }]);
    bot.emit('login', { id: 'wxid_bot' });
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'already_logged_in' }]);
    await bridge.settled();
    assert.deepEqual([bots.length, bot.calls], [1, ['start']], 'no refusal touched a bot');
    assert.equal((await json('/status'))[1].phase, 'logged_in');
  } finally { await bridge.close(); }
});

test('two requests at once start one bot', async () => {
  const { bots, bridge, json } = await started();
  try {
    const answers = await Promise.all([json('/login', { method: 'POST' }), json('/login', { method: 'POST' })]);
    assert.deepEqual(answers.map(([status]) => status).sort(), [202, 409]);
    await bridge.settled();
    assert.deepEqual([bots.length, bots[0].calls], [1, ['start']]);
  } finally { await bridge.close(); }
});

test('POST /login is rate limited: 2 minutes apart, 5 an hour and 12 a day, with retry_after_s', async () => {
  let clock = Date.parse('2026-11-20T12:00:00Z');
  const { bot, bots, bridge, json } = await started({ now: () => new Date(clock) });
  const expire = async () => { await bridge.settled(); bot().emit('scan', '', types.ScanStatus.Timeout); await bridge.settled(); };
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
    assert.equal(bots.length, 6, 'a refused request never built a bot');
    // Six more, one an hour (12 in all, the first at 12:00): the 13th waits until 12:00 tomorrow.
    for (let i = 0; i < 6; i++) { await expire(); clock += HOUR; assert.equal((await json('/login', { method: 'POST' }))[0], 202, `hour ${i}`); }
    await expire();
    clock += HOUR;
    const dayEnd = Date.parse('2026-11-21T12:00:00Z');
    assert.deepEqual(await json('/login', { method: 'POST' }), [429, { refused: 'rate_limited', retry_after_s: (dayEnd - clock) / 1000 }]);
    clock = dayEnd;
    assert.equal((await json('/login', { method: 'POST' }))[0], 202, 'the first is a day old');
    assert.equal(bots.length, 13);
  } finally { await bridge.close(); }
});

test('a bot that cannot be built or started, or a QR that never appears, ends the attempt as login_failed', async () => {
  const logs = [];
  let buildError;
  const { bots, bridge, json } = await started({ log: line => logs.push(line),
    loginLimits: { ...LOGIN_LIMITS, minGapMs: 0, qrWaitMs: 50 },
    prepare: () => { if (buildError) throw buildError; } });
  try {
    buildError = new Error('no native addon');
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    assert.deepEqual([bridge.state.phase, bridge.state.idle_reason, bots.length], ['idle', 'login_failed', 0]);
    assert.ok(logs.includes('building the puppet failed: no native addon'));
    buildError = undefined;
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    assert.equal(bridge.state.phase, 'starting');
    await until(() => bridge.state.phase === 'idle', 2000);
    assert.equal(bridge.state.idle_reason, 'login_failed', 'no QR within qrWaitMs');
    // A start that throws.
    const { bots: failing, bridge: other, json: otherJson } = await started({ log: line => logs.push(line),
      prepare: made => { made.startError = new Error('puppet start exploded'); } });
    try {
      assert.equal((await otherJson('/login', { method: 'POST' }))[0], 202);
      await other.settled();
      await until(() => other.state.phase === 'idle');
      assert.deepEqual([other.state.idle_reason, failing[0].calls], ['login_failed', ['start', 'stop']]);
      assert.ok(logs.includes('starting the puppet failed: puppet start exploded'));
    } finally { await other.close(); }
    // A QR that comes in time cancels that timer.
    assert.equal((await json('/login', { method: 'POST' }))[0], 202);
    await bridge.settled();
    bots.at(-1).emit('scan', 'https://login.weixin.qq.com/l/q', types.ScanStatus.Waiting);
    await new Promise(resolve => setTimeout(resolve, 120));
    assert.equal(bridge.state.phase, 'waiting_qr');
  } finally { await bridge.close(); }
});

test('with login disabled (WECHAT_BRIDGE_SKIP_LOGIN=1) every request is refused and no bot is built', async () => {
  const { bots, bridge, json } = await started({ loginEnabled: false });
  try {
    assert.deepEqual(await json('/login', { method: 'POST' }), [409, { refused: 'login_disabled' }]);
    assert.deepEqual(bots, []);
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

// --- step 23: the family group (design-steward 7.7.11) --------------------------------------
const ROOM = 'r1@chatroom';
const roomMessage = (options = {}) => message({ roomId: options.roomId ?? ROOM, from: 'wxid_m1', ...options });
const withRooms = async (s, rooms = [{ room_ref: ROOM, prefix: '@管家' }], owner = 'wxid_owner') =>
  assert.equal((await s.json('/rooms', { method: 'PUT', body: { owner_ref: owner, rooms } }))[0], 200);

test('group: the bridge keeps the group bounds as code constants', async () => {
  const { GROUP_SEND_LIMITS, GROUP_RING_SIZE, STRANGER_RING_SIZE } = await import('./bridge.mjs');
  assert.deepEqual({ ...GROUP_SEND_LIMITS }, { sendGapMs: 10000, backstopPerHour: 90 });
  assert.equal(Object.isFrozen(GROUP_SEND_LIMITS), true);
  assert.deepEqual([GROUP_RING_SIZE, STRANGER_RING_SIZE], [200, 50]);
});

test('group: a bound room\'s text passes only when it calls the bot, without the prefix; the rest is dropped', async () => {
  const s = await started();
  const bot = await s.login();
  await withRooms(s);
  bot.emit('message', roomMessage({ id: 'g1', text: '@管家 武侠那个项目做到哪了' }));
  bot.emit('message', roomMessage({ id: 'g2', text: '今天吃什么' }));
  bot.emit('message', roomMessage({ id: 'g3', text: '@管家 hi', self: true }));
  // A non-text message that is no describable medium (a video) is still dropped; the kinds
  // `mediaOf` takes are kept without a mention since design/steward-group-media.md §1 (below).
  bot.emit('message', roomMessage({ id: 'g4', text: '@管家 hi', type: types.Message.Video }));
  bot.emit('message', roomMessage({ id: 'g5', text: '@管家 hi', date: null }));
  bot.emit('message', roomMessage({ id: 'g6', text: '@别人 hi' }));
  const [, body] = await s.json('/events?after=0');
  await s.bridge.close();
  assert.deepEqual(body.events.map(e => [e.type, e.message_ref, e.text, e.at ?? null, e.asked]),
    [['group_message', 'g1', '武侠那个项目做到哪了', '2026-11-18T10:00:00.000Z', true], ['group_message', 'g5', 'hi', null, true]]);
  assert.equal(body.events[0].room_ref, ROOM);
  assert.equal(body.events[0].sender_ref, 'wxid_m1');
  assert.equal(body.events[0].attachments, undefined, 'a text message carries no attachments');
  await s.bridge.close();
});

test('group: an unbound room passes only "@prefix group-code"', async () => {
  const s = await started();
  const bot = await s.login();
  bot.emit('message', roomMessage({ id: 'b1', roomId: 'r9@chatroom', text: '@管家 G-7K2M-Q9XD' }));
  bot.emit('message', roomMessage({ id: 'b2', roomId: 'r9@chatroom', text: '@管家 武侠呢' }));
  bot.emit('message', roomMessage({ id: 'b3', roomId: 'r9@chatroom', text: '看 G-7K2M-Q9XD' }));
  const [, body] = await s.json('/events?after=0');
  assert.deepEqual(body.events.map(e => [e.type, e.room_ref, e.prefix, e.code]),
    [['group_bind', 'r9@chatroom', '@管家', 'G-7K2M-Q9XD']]);
  await s.bridge.close();
});

test('group: a full group ring and 600 strangers never push the owner\'s 1:1 messages out', async () => {
  const s = await started({ groupRingSize: 5, strangerRingSize: 3, known: ['wxid_owner', 'wxid_x'] });
  const bot = await s.login();
  await withRooms(s);
  bot.emit('message', message({ id: 'own1', text: 'from the owner' }));
  for (let i = 0; i < 20; i++) bot.emit('message', roomMessage({ id: 'g' + i, text: '@管家 q' + i }));
  for (let i = 0; i < 600; i++) bot.emit('message', message({ id: 's' + i, from: 'wxid_x', text: 'hello' }));
  const [, body] = await s.json('/events?after=0');
  const kinds = body.events.map(e => e.message_ref);
  assert.ok(kinds.includes('own1'));
  assert.equal(kinds.filter(k => k.startsWith('g')).length, 5);
  assert.equal(kinds.filter(k => k.startsWith('s')).length, 3);
  // One seq across the rings, in order.
  assert.deepEqual(body.events.map(e => e.seq), [...body.events.map(e => e.seq)].sort((a, b) => a - b));
  await s.bridge.close();
});

test('group: sends go to bound rooms only, one queue with the gap, and a room\'s backstop answers 429', async () => {
  let clock = Date.parse('2026-11-21T10:00:00Z');
  const slept = [];
  const s = await started({ now: () => new Date(clock), sleep: async ms => { slept.push(ms); clock += ms; },
    sendLimits: { sendGapMs: 10000, backstopPerHour: 3 },
    prepare: bot => { bot.Room = { load: id => ({ say: async text => { bot.said.push({ room: id, text }); return { id: 'gr' + bot.said.length }; } }) }; } });
  const bot = await s.login();
  assert.equal((await s.json('/send', { method: 'POST', body: { room: ROOM, text: 'x' } }))[1].error, 'room_not_bound');
  await withRooms(s);
  const sends = await Promise.all([1, 2, 3, 4].map(i => s.json('/send', { method: 'POST', body: { room: ROOM, text: 'a' + i } })));
  assert.deepEqual(sends.map(([status]) => status), [200, 200, 200, 429]);
  assert.deepEqual(bot.said.map(x => x.text), ['a1', 'a2', 'a3']);
  assert.deepEqual(slept, [10000, 10000]);
  await s.bridge.close();
});

test('group: a message whose room() throws still reaches the ring, because the payload carries the room id', async () => {
  // The live puppet's own message: `room()` raises the TypeError Wechaty's mixin raises
  // when the puppet answers no room payload, and `payload.roomId` is the room information
  // that survives. The throwing `room()` is the witness: a bridge that calls it loses the
  // message (and logs `message skipped`), so both paths are exercised here.
  const live = (id, roomId) => ({
    id, self: () => false, type: () => types.Message.Text, text: () => '@管家 武侠那个项目做到哪了',
    date: () => new Date('2026-11-18T10:00:00Z'), payload: roomId ? { roomId } : {},
    talker: () => ({ id: 'wxid_m1', type: () => types.Contact.Unknown }),
    room() { throw new TypeError('this.roomRawPayload is not a function'); },
  });
  const logs = [];
  const s = await started({ log: line => logs.push(line) });
  try {
    const bot = await s.login();
    await withRooms(s);
    bot.emit('message', live('live-group', ROOM));
    bot.emit('message', live('live-direct'));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.type, e.room_ref ?? null, e.sender_ref, e.message_ref, e.text]), [
      ['group_message', ROOM, 'wxid_m1', 'live-group', '武侠那个项目做到哪了'],
      ['message', null, 'wxid_m1', 'live-direct', '@管家 武侠那个项目做到哪了'],
    ]);
    assert.ok(!logs.some(line => line.startsWith('message skipped')), logs.join('\n'));
    assert.equal(s.bridge.state.phase, 'logged_in');
  } finally { await s.bridge.close(); }
});

// --- step 24.2: media (design-steward 7.8.12 "微信") -----------------------------------------
// The fork as the platform director gave its interface: messageFile / messageImage / messageVoice
// hand a FileBox over (a voice as WAV); messageSendFile(conversationId, fileBox) sends one, the
// kind taken from the FileBox name. These fakes stand in for it.
const b64 = text => Buffer.from(text, 'utf8').toString('base64');
const XML = {
  image: (hd = 1000) => `<?xml version="1.0"?><msg><img aeskey="k" length="900" hdlength="${hd}" md5="x"></img></msg>`,
  voice: (length = 3000) => `<msg><voicemsg endflag="1" length="${length}" voicelength="2300" /></msg>`,
  file: (total = 5000, name = '报告 &amp; 附录.pdf', type = 6) => `<msg><appmsg appid="" sdkver="0"><title>${name}</title><des></des>`
    + `<type>${type}</type><appattach><totallen>${total}</totallen><fileext>pdf</fileext></appattach></appmsg></msg>`,
  quote: () => '<msg><appmsg appid="" sdkver="0"><title>好的</title><type>57</type><refermsg><type>1</type>'
    + '<content>同意 proposal p-1</content></refermsg></appmsg></msg>',
};
const KIND_TYPE = { image: [types.Message.Image, 3], voice: [types.Message.Audio, 34], file: [types.Message.Attachment, 49] };
/** A PCM WAV header naming `rate` bytes a second (`channels` x `bits`), then `dataBytes` of
 *  zeros; wav(32000, n) is 16 kHz mono 16-bit, the shape the fork's voice bar takes. */
const wav = (rate, dataBytes, { channels = 1, bits = 16 } = {}) => {
  const head = Buffer.alloc(44), align = channels * bits / 8;
  head.write('RIFF', 0, 'latin1'); head.writeUInt32LE(36 + dataBytes, 4); head.write('WAVE', 8, 'latin1');
  head.write('fmt ', 12, 'latin1'); head.writeUInt32LE(16, 16); head.writeUInt16LE(1, 20); head.writeUInt16LE(channels, 22);
  head.writeUInt32LE(rate / align, 24); head.writeUInt32LE(rate, 28); head.writeUInt16LE(align, 32); head.writeUInt16LE(bits, 34);
  head.write('data', 36, 'latin1'); head.writeUInt32LE(dataBytes, 40);
  return Buffer.concat([head, Buffer.alloc(dataBytes)]);
};
/** A JPEG's first bytes (FF D8 FF E0) padded to `size` bytes. */
const jpeg = size => Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(size - 4, 7)]);
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
/** A 1:1 (or `roomId`) media message: its payload as the fork carries it, its text (`text`, a
 *  caption in a group), and the FileBox its fetch hands over (`bytes` in `chunk`-sized parts;
 *  `gate` holds the stream until resolved; `fail` makes the first `fails` fetches throw it;
 *  `hang` makes them never answer; `stall` makes the stream stop after its first part, never
 *  ending). `fetches` counts the fetches; `destroyed` counts the streams the bridge stopped. */
function mediaMessage({ kind, id = 'p1', from = 'wxid_owner', text = '', xml, raw, bytes = Buffer.from('JPEGDATA'), chunk = 4096,
  gate, fail, fails = Infinity, hang, stall, roomId, name = 'x.jpg', type } = {}) {
  const [wechatyType, wechatType] = KIND_TYPE[kind] ?? [type, raw];
  const fetched = { count: 0, via: [], destroyed: 0 };
  const box = () => ({ name, mediaType: kind === 'voice' ? 'audio/wav' : 'image/jpeg', toStream: async () => {
    if (gate) await gate;
    const parts = [];
    for (let i = 0; i < bytes.length; i += chunk) parts.push(bytes.subarray(i, i + chunk));
    const stream = stall ? new Readable({ read() { if (parts.length) this.push(parts.shift()); parts.length = 0; } }) : Readable.from(parts);
    stream.on('close', () => { fetched.destroyed++; });
    return stream;
  } });
  const fetch = via => async () => {
    fetched.count++; fetched.via.push(via);
    if (hang && fetched.count <= fails) return new Promise(() => {});
    if (fail && fetched.count <= fails) throw new Error(fail);
    return box();
  };
  const base = message({ id, from, text, type: type ?? wechatyType, roomId });
  return Object.assign(base, { fetched, payload: { ...base.payload, messageType: raw ?? wechatType, ...(xml ? { content: b64(xml) } : {}) },
    toFileBox: fetch('messageFile'), toImage: () => ({ hd: fetch('messageImage') }) });
}
const makeFileBox = (bytes, name) => ({ name, bytes: Buffer.from(bytes) });
const mediaBridge = async (options = {}) => {
  const tmp = mkdtempSync(join(tmpdir(), 'wechat-bridge-media-'));
  const s = await started({ tmpDir: tmp, makeFileBox, ...options });
  return { ...s, tmp, tmpEmpty: () => readdirSync(tmp).length === 0 };
};
const fetchMedia = async (s, mediaId, token = TOKEN) => {
  const r = await s.call('/media/' + mediaId, { token });
  return [r.status, Buffer.from(await r.arrayBuffer()), r.headers.get('content-type'), r.headers.get('x-voice-duration-ms')];
};
const postFile = (s, query, bytes, token = TOKEN) => fetch(`http://${s.address.address}:${s.address.port}/send-file?${new URLSearchParams(query)}`, {
  method: 'POST', body: bytes, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), 'content-type': 'application/octet-stream' } })
  .then(async r => [r.status, await r.json()]);

test('media: the bounds are code constants', () => {
  assert.deepEqual({ ...MEDIA_LIMITS }, { image: 20 * 1048576, file: 25 * 1048576, voiceSource: 2 * 1048576, voiceMs: 125000,
    voiceWavMax: 12 * 1048576, voiceWavUnknown: 6 * 1048576, sendBody: 25 * 1048576 });
  assert.equal(Object.isFrozen(MEDIA_LIMITS), true);
});

test('media: 1:1 images, voices and files become events with descriptions only; nothing is fetched on arrival', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const image = mediaMessage({ kind: 'image', id: 'i1', xml: XML.image(1234) });
    const voice = mediaMessage({ kind: 'voice', id: 'v1', xml: XML.voice(3000) });
    const file = mediaMessage({ kind: 'file', id: 'f1', xml: XML.file(5000) });
    const bare = mediaMessage({ kind: 'image', id: 'i2' }); // a payload without XML: no size, no name
    for (const m of [image, voice, file, bare]) bot.emit('message', m);
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, e.text, e.attachments]), [
      ['i1', '', [{ media_id: `${body.events[0].seq}.0`, kind: 'image', size: 1234 }]],
      ['v1', '', [{ media_id: `${body.events[1].seq}.0`, kind: 'voice', mime: 'audio/wav', size: 3000 }]],
      ['f1', '', [{ media_id: `${body.events[2].seq}.0`, kind: 'file', name: '报告 & 附录.pdf', size: 5000 }]],
      ['i2', '', [{ media_id: `${body.events[3].seq}.0`, kind: 'image' }]],
    ]);
    assert.deepEqual([image, voice, file, bare].map(m => m.fetched.count), [0, 0, 0, 0], 'no fetch before GET /media');
    assert.ok(s.tmpEmpty(), 'tmpfs is empty');
    assert.equal(s.bridge.live, 4);
  } finally { await s.bridge.close(); }
});

test('media: over the size WeChat reports, a description is too_large and has no media_id', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    bot.emit('message', mediaMessage({ kind: 'image', id: 'i', xml: XML.image(20 * 1048576 + 1) }));
    bot.emit('message', mediaMessage({ kind: 'voice', id: 'v', xml: XML.voice(2 * 1048576 + 1) }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'f', xml: XML.file(25 * 1048576 + 1) }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'ok', xml: XML.file(25 * 1048576) }));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, e.attachments[0].too_large ?? false, 'media_id' in e.attachments[0]]),
      [['i', true, false], ['v', true, false], ['f', true, false], ['ok', false, true]]);
    assert.equal(s.bridge.live, 1);
    assert.equal((await fetchMedia(s, `${body.events[0].seq}.0`))[0], 404, 'a too_large one is not live');
  } finally { await s.bridge.close(); }
});

test('media: a file the fork types Unknown is taken by its raw 49 and the appmsg XML; a 49 whose XML cannot be read is dropped', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const unknownFile = mediaMessage({ type: types.Message.Unknown, raw: 49, id: 'unknown-49-file', xml: XML.file(10), bytes: Buffer.from('%PDF') });
    bot.emit('message', unknownFile);
    bot.emit('message', mediaMessage({ type: types.Message.Unknown, raw: 49, id: 'unknown-49-bare' }));
    bot.emit('message', mediaMessage({ type: types.Message.Unknown, raw: 49, id: 'unknown-49-link', xml: XML.file(10, 'a link', 5) }));
    bot.emit('message', mediaMessage({ type: types.Message.Attachment, raw: 49, id: 'no-content' }));
    const notXml = mediaMessage({ type: types.Message.Attachment, raw: 49, id: 'not-xml' });
    notXml.payload.content = b64('compressed appmsg, no markup');
    bot.emit('message', notXml);
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, e.attachments]), [
      ['unknown-49-file', [{ media_id: `${body.events[0].seq}.0`, kind: 'file', name: '报告 & 附录.pdf', size: 10 }]]]);
    const [status, bytes] = await fetchMedia(s, body.events[0].attachments[0].media_id);
    assert.deepEqual([status, bytes.toString()], [200, '%PDF']);
    assert.deepEqual(unknownFile.fetched.via, ['messageFile']);
    assert.equal(mediaOf(mediaMessage({ type: types.Message.Attachment, raw: 49 }), types), undefined, 'no XML: not let in');
  } finally { await s.bridge.close(); }
});

test('media: quoted replies (49/57), other app messages, videos and stickers are still dropped', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    bot.emit('message', mediaMessage({ type: types.Message.Unknown, raw: 49, id: 'quote-unknown', xml: XML.quote() }));
    bot.emit('message', mediaMessage({ type: types.Message.Attachment, raw: 49, id: 'quote-as-file', xml: XML.quote() }));
    bot.emit('message', mediaMessage({ type: types.Message.Attachment, raw: 57, id: 'type-57' }));
    bot.emit('message', mediaMessage({ type: types.Message.Attachment, raw: 49, id: 'link', xml: XML.file(10, 'a link', 5) }));
    bot.emit('message', mediaMessage({ type: types.Message.Attachment, raw: 49, id: 'file-quoting',
      xml: XML.file(10).replace('</appmsg>', '<refermsg><content>同意</content></refermsg></appmsg>') }));
    bot.emit('message', mediaMessage({ type: types.Message.Image, raw: 47, id: 'sticker-as-image' }));
    bot.emit('message', mediaMessage({ type: types.Message.Video, raw: 43, id: 'video' }));
    bot.emit('message', mediaMessage({ type: types.Message.Emoticon, raw: 47, id: 'sticker' }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'real-file', xml: XML.file(10) }));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => e.message_ref), ['real-file']);
    assert.equal(mediaOf(mediaMessage({ type: types.Message.Unknown, raw: 57 }), types), undefined);
  } finally { await s.bridge.close(); }
});

// design/steward-group-media.md §1 (the owner's ruling of 2026-11-22): a medium that arrives
// in a bound group WITHOUT calling the steward is still described and kept, so a later
// question can read it; only an unbound room keeps refusing everything but a group code.
test('group media: an un-mentioned image in a bound room is one group_message with its attachment, text "" and asked false', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    await withRooms(s);
    const bytes = Buffer.from('PHOTO');
    const photo = mediaMessage({ kind: 'image', id: 'un-asked', roomId: ROOM, from: 'wxid_m1', xml: XML.image(5), bytes });
    bot.emit('message', photo);
    const [, body] = await s.json('/events?after=0');
    const [event] = body.events;
    assert.equal(body.events.length, 1, 'one event, one message');
    assert.deepEqual([event.type, event.room_ref, event.sender_ref, event.message_ref, event.text, event.asked],
      ['group_message', ROOM, 'wxid_m1', 'un-asked', '', false]);
    // The attachment is the 1:1 description, registered in `live` under this event's own media_id.
    assert.deepEqual(event.attachments, [{ media_id: `${event.seq}.0`, kind: 'image', size: 5 }]);
    assert.equal(photo.fetched.count, 0, 'nothing is fetched on arrival');
    assert.ok(s.tmpEmpty(), 'tmpfs is empty');
    const [status, got, type] = await fetchMedia(s, event.attachments[0].media_id);
    assert.deepEqual([status, got.equals(bytes), type], [200, true, 'image/jpeg']);
    assert.deepEqual(photo.fetched.via, ['messageImage']);
    assert.ok(s.tmpEmpty());
  } finally { await s.bridge.close(); }
});

test('group media: a mention with a caption keeps the attachment and carries the stripped text, asked true', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    await withRooms(s);
    const bytes = Buffer.from('CAPTIONED');
    const photo = mediaMessage({ kind: 'image', id: 'asked-with-photo', roomId: ROOM, from: 'wxid_m1',
      text: '@管家 这个图上是什么', bytes });
    bot.emit('message', photo);
    const [, body] = await s.json('/events?after=0');
    const [event] = body.events;
    assert.equal(body.events.length, 1);
    assert.deepEqual([event.text, event.asked, event.attachments.map(a => a.kind)], ['这个图上是什么', true, ['image']]);
    const [status, got] = await fetchMedia(s, event.attachments[0].media_id);
    assert.deepEqual([status, got.equals(bytes)], [200, true]);
  } finally { await s.bridge.close(); }
});

test('group media: a bound room\'s voice and file are described exactly as 1:1 ones are', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    await withRooms(s);
    bot.emit('message', mediaMessage({ kind: 'voice', id: 'v', roomId: ROOM, from: 'wxid_m1', xml: XML.voice(3000) }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'f', roomId: ROOM, from: 'wxid_m1', xml: XML.file(5000) }));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.type, e.message_ref, e.text, e.asked, e.attachments]), [
      ['group_message', 'v', '', false, [{ media_id: `${body.events[0].seq}.0`, kind: 'voice', mime: 'audio/wav', size: 3000 }]],
      ['group_message', 'f', '', false, [{ media_id: `${body.events[1].seq}.0`, kind: 'file', name: '报告 & 附录.pdf', size: 5000 }]],
    ]);
    assert.equal(s.bridge.live, 2);
  } finally { await s.bridge.close(); }
});

test('group media: over the bound keeps the 1:1 shape (too_large, no media_id), and the group ring evicts its own', async () => {
  const s = await mediaBridge({ groupRingSize: 2 });
  try {
    const bot = await s.login();
    await withRooms(s);
    bot.emit('message', mediaMessage({ kind: 'image', id: 'huge', roomId: ROOM, from: 'wxid_m1', xml: XML.image(20 * 1048576 + 1) }));
    bot.emit('message', mediaMessage({ kind: 'image', id: 'keep', roomId: ROOM, from: 'wxid_m1', bytes: Buffer.from('KEEP') }));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, e.attachments[0].too_large ?? false, 'media_id' in e.attachments[0]]),
      [['huge', true, false], ['keep', false, true]]);
    assert.equal(s.bridge.live, 1, 'a too_large one is never live');
    const kept = body.events[1].attachments[0].media_id;
    assert.deepEqual((await fetchMedia(s, kept)).slice(0, 1), [200]);
    // Two more group messages push 'keep' off the group ring; its media_id leaves `live` with it.
    bot.emit('message', roomMessage({ id: 'later-1', text: '@管家 q1' }));
    bot.emit('message', roomMessage({ id: 'later-2', text: '@管家 q2' }));
    assert.equal(s.bridge.live, 0);
    assert.deepEqual(await s.json('/media/' + kept), [404, { error: 'not_found' }]);
  } finally { await s.bridge.close(); }
});

test('group media: an unbound room\'s image still yields nothing, mentioned or not', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const bytes = Buffer.from('A ROOM THIS HOST DOES NOT SERVE');
    bot.emit('message', mediaMessage({ kind: 'image', id: 'unbound-plain', roomId: 'r9@chatroom', from: 'wxid_m1', bytes }));
    bot.emit('message', mediaMessage({ kind: 'image', id: 'unbound-captioned', roomId: 'r9@chatroom', from: 'wxid_m1',
      text: '@管家 看这个', bytes }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'unbound-file', roomId: 'r9@chatroom', from: 'wxid_m1', xml: XML.file(10) }));
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events, []);
    assert.equal(s.bridge.live, 0);
  } finally { await s.bridge.close(); }
});

test('media: GET /media fetches on demand, may be repeated, answers the same bytes and leaves tmpfs empty', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const bytes = Buffer.from('a jpeg, or so it says '.repeat(1000));
    const image = mediaMessage({ kind: 'image', id: 'i1', bytes, chunk: 1000 });
    const file = mediaMessage({ kind: 'file', id: 'f1', xml: XML.file(10), bytes: Buffer.from('%PDF-1.7') });
    bot.emit('message', image);
    bot.emit('message', file);
    const [, body] = await s.json('/events?after=0');
    const [imageId, fileId] = body.events.map(e => e.attachments[0].media_id);
    assert.equal((await fetchMedia(s, imageId, null))[0], 401, 'the same token');
    assert.equal(image.fetched.count, 0);
    const first = await fetchMedia(s, imageId);
    const second = await fetchMedia(s, imageId);
    assert.deepEqual([first[0], second[0]], [200, 200]);
    assert.ok(first[1].equals(bytes) && second[1].equals(bytes), 'both fetches answer the same bytes');
    assert.equal(first[2], 'image/jpeg');
    assert.deepEqual(image.fetched.via, ['messageImage', 'messageImage']);
    const [status, got] = await fetchMedia(s, fileId);
    assert.equal(status, 200);
    assert.equal(got.toString(), '%PDF-1.7');
    assert.deepEqual(file.fetched.via, ['messageFile']);
    assert.ok(s.tmpEmpty(), 'nothing left in tmpfs');
    assert.equal((await fetchMedia(s, '999.0'))[0], 404);
  } finally { await s.bridge.close(); }
});

test('media: more bytes than the bound are cut: 413 too_large, the source stopped, no partial left', async () => {
  const s = await mediaBridge({ mediaLimits: { ...MEDIA_LIMITS, image: 10_000, file: 20_000 } });
  try {
    const bot = await s.login();
    // Reported small, really large: the report is not trusted.
    bot.emit('message', mediaMessage({ kind: 'image', id: 'big', xml: XML.image(100), bytes: Buffer.alloc(50_000), chunk: 1000 }));
    bot.emit('message', mediaMessage({ kind: 'image', id: 'edge', bytes: Buffer.alloc(10_000), chunk: 1000 }));
    bot.emit('message', mediaMessage({ kind: 'file', id: 'file', xml: XML.file(10), bytes: Buffer.alloc(20_001), chunk: 1000 }));
    const [, body] = await s.json('/events?after=0');
    const ids = body.events.map(e => e.attachments[0].media_id);
    assert.deepEqual((await fetchMedia(s, ids[0])).slice(0, 1), [413]);
    assert.ok(s.tmpEmpty(), 'the partial is deleted');
    const [edgeStatus, edge] = await fetchMedia(s, ids[1]);
    assert.deepEqual([edgeStatus, edge.length], [200, 10_000], 'exactly the bound passes');
    assert.equal((await fetchMedia(s, ids[2]))[0], 413);
    assert.ok(s.tmpEmpty());
  } finally { await s.bridge.close(); }
});

test('media: a voice comes as WAV from messageVoice, held to 2 min 5 s by its header\'s rate, its length from the fork\'s name', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const voices = new Map();
    // The fork names the WAV '<durationMs>ms.wav' (here the 'short' one; the rest keep their id).
    bot.puppet.messageVoice = async id => {
      const bytes = voices.get(id);
      return { name: id === 'short' ? '59990ms.wav' : id + '.wav', toStream: async () => Readable.from([bytes.subarray(0, 100), bytes.subarray(100)]) };
    };
    // 2000 B/s: 125 s is 250000 bytes of audio (plus the header's slack).
    voices.set('short', wav(2000, 2000 * 60));
    voices.set('long', wav(2000, 2000 * 130));
    voices.set('not-wav', Buffer.alloc(6 * 1048576 + 1));
    for (const id of voices.keys()) bot.emit('message', mediaMessage({ kind: 'voice', id, xml: XML.voice(1000) }));
    const [, body] = await s.json('/events?after=0');
    const ids = body.events.map(e => e.attachments[0].media_id);
    const [status, bytes, type, durationMs] = await fetchMedia(s, ids[0]);
    assert.deepEqual([status, type, durationMs], [200, 'audio/wav', '59990'], 'the length the name states, not the header\'s 60000');
    assert.ok(bytes.equals(voices.get('short')));
    assert.equal((await fetchMedia(s, ids[1]))[0], 413, 'longer than 2 min 5 s');
    assert.equal((await fetchMedia(s, ids[2]))[0], 413, 'no WAV header: the 6 MiB bound');
    assert.ok(s.tmpEmpty());
    assert.equal(wavByteCap(wav(48000, 0)), 4096 + 48000 * 125);
    assert.equal(wavByteCap(wav(1_000_000, 0)), MEDIA_LIMITS.voiceWavMax);
    // Without messageVoice the puppet's messageFile serves it.
    delete bot.puppet.messageVoice;
    const plain = mediaMessage({ kind: 'voice', id: 'plain', xml: XML.voice(10), bytes: wav(2000, 100) });
    bot.emit('message', plain);
    const [, later] = await s.json(`/events?after=${body.next_after}`);
    const answer = await fetchMedia(s, later.events[0].attachments[0].media_id);
    assert.deepEqual([answer[0], answer[3]], [200, '50'], 'a name without the length: 100 bytes at 2000 B/s');
    assert.deepEqual(plain.fetched.via, ['messageFile']);
    assert.equal(inboundVoiceMs('1200ms.wav', Buffer.alloc(0), 0), 1200);
    assert.equal(inboundVoiceMs('0ms.wav', wav(32000, 3200), 3244), 100, 'no length in the name: the header');
    assert.equal(inboundVoiceMs('note.wav', Buffer.from('not a wav'), 9), undefined);
  } finally { await s.bridge.close(); }
});

test('media: one fetch at a time - another request meanwhile is 503 busy', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    bot.emit('message', mediaMessage({ kind: 'image', id: 'slow', gate }));
    bot.emit('message', mediaMessage({ kind: 'image', id: 'other' }));
    const [, body] = await s.json('/events?after=0');
    const [slow, other] = body.events.map(e => e.attachments[0].media_id);
    const pending = fetchMedia(s, slow);
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual((await s.json('/media/' + other))[0], 503);
    assert.deepEqual(await s.json('/media/' + other), [503, { error: 'busy' }]);
    release();
    assert.equal((await pending)[0], 200);
    assert.equal((await fetchMedia(s, other))[0], 200, 'free again once the first answer ended');
    assert.ok(s.tmpEmpty());
  } finally { await s.bridge.close(); }
});

test('media: a fetch the puppet never answers, or whose stream stalls, ends at its deadline: 503, the lock released, nothing left', async () => {
  // Read at each request: short for the two that must time out, then long again, so a loaded
  // machine never times out the fetches that must succeed.
  const limits = { firstByteMs: 100, totalMs: 2000, attempts: 5 };
  const s = await mediaBridge({ fetchLimits: limits });
  try {
    const bot = await s.login();
    const hung = mediaMessage({ kind: 'image', id: 'hung', hang: true, fails: 1 });
    const stalled = mediaMessage({ kind: 'image', id: 'stalled', stall: true, bytes: Buffer.alloc(3000), chunk: 1000 });
    bot.emit('message', hung);
    bot.emit('message', stalled);
    bot.emit('message', mediaMessage({ kind: 'image', id: 'fine', bytes: Buffer.from('FINE') }));
    const [, body] = await s.json('/events?after=0');
    const [hungId, stalledId, fineId] = body.events.map(e => e.attachments[0].media_id);
    let t0 = Date.now();
    assert.deepEqual(await s.json('/media/' + hungId), [503, { error: 'unavailable' }], 'no first byte in time');
    assert.ok(Date.now() - t0 < 1500, 'the first-byte deadline, not the total one');
    assert.ok(s.tmpEmpty());
    t0 = Date.now();
    assert.deepEqual(await s.json('/media/' + stalledId), [503, { error: 'unavailable' }], 'a first byte, then never the rest');
    assert.ok(Date.now() - t0 >= 1900, 'the first byte cleared the first-byte deadline; the total one ended it');
    await until(() => stalled.fetched.destroyed === 1);
    assert.ok(s.tmpEmpty());
    Object.assign(limits, MEDIA_FETCH);
    const [status, bytes] = await fetchMedia(s, fineId);
    assert.deepEqual([status, bytes.toString()], [200, 'FINE'], 'the lock is free again');
    const [again, hungBytes] = await fetchMedia(s, hungId);
    assert.deepEqual([again, hungBytes.toString()], [200, 'JPEGDATA'], 'a timed-out fetch is transient: asked again, it is served');
    assert.ok(s.tmpEmpty());
  } finally { await s.bridge.close(); }
});

test('media: a client that goes away releases the lock at once: the next GET /media is served, not 503', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const hung = mediaMessage({ kind: 'image', id: 'hung', hang: true });
    bot.emit('message', hung);
    bot.emit('message', mediaMessage({ kind: 'image', id: 'next', bytes: Buffer.from('NEXT') }));
    const [, body] = await s.json('/events?after=0');
    const [hungId, nextId] = body.events.map(e => e.attachments[0].media_id);
    const client = new AbortController();
    const raw = fetch(`http://${s.address.address}:${s.address.port}/media/${hungId}`,
      { headers: { authorization: `Bearer ${TOKEN}` }, signal: client.signal }).catch(error => error);
    await until(() => hung.fetched.count === 1);
    assert.deepEqual((await s.json('/media/' + nextId))[0], 503, 'busy while the first fetch runs');
    client.abort();
    await raw;
    await until(() => s.tmpEmpty());
    const [status, bytes] = await fetchMedia(s, nextId);
    assert.deepEqual([status, bytes.toString()], [200, 'NEXT']);
  } finally { await s.bridge.close(); }
});

test('media: a passing puppet error is 503 (held, asked again); a local tmpfs error is 503 and never asks the puppet; repeated failures end as 410', async () => {
  const s = await mediaBridge({ fetchLimits: { ...MEDIA_FETCH, attempts: 3 } });
  try {
    const bot = await s.login();
    const once = mediaMessage({ kind: 'image', id: 'once', fail: 'CDN hiccup', fails: 1, bytes: Buffer.from('LATER') });
    const always = mediaMessage({ kind: 'file', id: 'always', xml: XML.file(10), fail: 'socket hang up' });
    bot.emit('message', once);
    bot.emit('message', always);
    const [, body] = await s.json('/events?after=0');
    const [onceId, alwaysId] = body.events.map(e => e.attachments[0].media_id);
    assert.deepEqual(await s.json('/media/' + onceId), [503, { error: 'unavailable' }]);
    const [status, bytes] = await fetchMedia(s, onceId);
    assert.deepEqual([status, bytes.toString()], [200, 'LATER'], 'asked again, it is served');
    assert.deepEqual(await s.json('/media/' + alwaysId), [503, { error: 'unavailable' }]);
    assert.deepEqual(await s.json('/media/' + alwaysId), [503, { error: 'unavailable' }]);
    assert.deepEqual(await s.json('/media/' + alwaysId), [410, { error: 'gone' }], 'the third failure: gone');
    assert.deepEqual(await s.json('/media/' + alwaysId), [410, { error: 'gone' }]);
    assert.equal(always.fetched.count, 3, 'once gone, the puppet is not asked again');
    assert.ok(s.tmpEmpty());
  } finally { await s.bridge.close(); }
  // tmpfs cannot take the file (here: no such directory): the bridge's fault, not the media's.
  const broken = await mediaBridge({ tmpDir: join(tmpdir(), 'wechat-bridge-no-such-dir-' + process.pid), fetchLimits: { ...MEDIA_FETCH, attempts: 1 } });
  try {
    const bot = await broken.login();
    const photo = mediaMessage({ kind: 'image', id: 'photo' });
    bot.emit('message', photo);
    const [, body] = await broken.json('/events?after=0');
    const id = body.events[0].attachments[0].media_id;
    assert.deepEqual(await broken.json('/media/' + id), [503, { error: 'unavailable' }]);
    assert.deepEqual(await broken.json('/media/' + id), [503, { error: 'unavailable' }], 'never counted against the media');
    assert.equal(photo.fetched.count, 0, 'the puppet was not asked');
  } finally { await broken.bridge.close(); }
});

test('media: a media_id that is not \'<seq>.<index>\' is 404, a malformed escape included', async () => {
  const s = await mediaBridge();
  try {
    await s.login();
    for (const path of ['/media/%E0%A4%A', '/media/abc', '/media/1.0.0', '/media/%31.0'])
      assert.deepEqual(await s.json(path), [404, { error: 'not_found' }], path);
  } finally { await s.bridge.close(); }
});

test('media: a puppet refusal is 410 gone; after a new login an old media_id is 410 and the puppet is not asked', async () => {
  // A second login request must wait out POST /login's 2-minute gap: the clock moves.
  let clock = Date.parse('2026-11-22T10:00:00Z');
  const s = await mediaBridge({ now: () => new Date(clock) });
  try {
    const bot = await s.login();
    const refused = mediaMessage({ kind: 'file', id: 'refused', xml: XML.file(10), fail: 'Neo has no observed peer for this message' });
    const old = mediaMessage({ kind: 'image', id: 'old' });
    bot.emit('message', refused);
    bot.emit('message', old);
    const [, body] = await s.json('/events?after=0');
    const [refusedId, oldId] = body.events.map(e => e.attachments[0].media_id);
    assert.deepEqual(await s.json('/media/' + refusedId), [410, { error: 'gone' }]);
    assert.ok(s.tmpEmpty());
    // The delivered fork's own refusals of a message it will never hand over are gone too; its
    // "not logged in" is not (a new login makes the media_id 410 anyway).
    assert.ok(isGoneError(new Error('Neo message payload is no longer cached')));
    assert.ok(isGoneError(new Error('Neo cannot fetch an image from a WeChat message of type 34')));
    assert.ok(!isGoneError(new Error('Neo is not logged in to WeChat: cannot fetch a file')));
    // Its refusals of a payload that can never be fetched are gone as well (never retried).
    for (const message of ['Neo app message is of type 57, not a file',
      "Neo image payload carries no encrypted picture on WeChat's CDN",
      "Neo file payload carries no encrypted attachment on WeChat's CDN",
      'Neo voice payload states no byte length to fetch'])
      assert.ok(isGoneError(new Error(message)), message);
    assert.ok(!isGoneError(new Error('Neo session is offline')));
    bot.emit('logout');
    assert.equal(s.bridge.state.phase, 'idle');
    assert.deepEqual(await s.json('/media/' + oldId), [410, { error: 'gone' }], 'logged out');
    clock += 3 * 60_000;
    const next = await s.login();
    assert.notEqual(next, bot);
    assert.deepEqual(await s.json('/media/' + oldId), [410, { error: 'gone' }], 'another login generation');
    assert.equal(old.fetched.count, 0, 'the puppet was never asked');
  } finally { await s.bridge.close(); }
});

test('media: an event cut off the ring takes its live entry with it (404); live never outnumbers the ring', async () => {
  const s = await mediaBridge({ ringSize: 3 });
  try {
    const bot = await s.login();
    bot.emit('message', mediaMessage({ kind: 'image', id: 'p1' }));
    bot.emit('message', message({ id: 't1', text: 'text' }));
    bot.emit('message', mediaMessage({ kind: 'image', id: 'p2' }));
    const [, first] = await s.json('/events?after=0');
    const p1 = first.events[0].attachments[0].media_id;
    assert.equal(s.bridge.live, 2);
    for (let i = 3; i <= 6; i++) {
      bot.emit('message', mediaMessage({ kind: 'image', id: 'p' + i }));
      const [, now] = await s.json('/events?after=0');
      assert.ok(s.bridge.live <= now.events.filter(e => e.attachments?.[0]?.media_id).length, 'live ≤ ring entries with a media_id');
    }
    assert.deepEqual(await s.json('/media/' + p1), [404, { error: 'not_found' }]);
    assert.equal(s.bridge.live, 3);
  } finally { await s.bridge.close(); }
});

test('media: with owner_ref set a stranger\'s files are descriptions with gone and no media_id, never fetched; unset they share the ring', async () => {
  const s = await mediaBridge({ known: ['wxid_owner', 'wxid_x'] });
  try {
    const bot = await s.login();
    const early = mediaMessage({ kind: 'file', id: 'before-binding', from: 'wxid_x', xml: XML.file(10) });
    bot.emit('message', early);
    await withRooms(s, [], 'wxid_owner');
    const late = mediaMessage({ kind: 'file', id: 'after-binding', from: 'wxid_x', xml: XML.file(25 * 1048576 + 5) });
    bot.emit('message', late);
    const [, body] = await s.json('/events?after=0');
    assert.deepEqual(body.events.map(e => [e.message_ref, e.attachments[0]]), [
      ['before-binding', { media_id: `${body.events[0].seq}.0`, kind: 'file', name: '报告 & 附录.pdf', size: 10 }],
      ['after-binding', { kind: 'file', name: '报告 & 附录.pdf', size: 25 * 1048576 + 5, gone: true }],
    ]);
    assert.equal(s.bridge.live, 1, 'the stranger buffer holds no live message');
    assert.equal(late.fetched.count + early.fetched.count, 0);
  } finally { await s.bridge.close(); }
});

test('media: an owner\'s held photo survives 600 strangers (half with files): still in the ring and fetchable', async () => {
  const s = await mediaBridge({ known: ['wxid_owner', 'wxid_x'] });
  try {
    const bot = await s.login();
    await withRooms(s, [], 'wxid_owner');
    const photo = mediaMessage({ kind: 'image', id: 'owner-photo', bytes: Buffer.from('PHOTO') });
    bot.emit('message', photo);
    for (let i = 0; i < 600; i++)
      bot.emit('message', i % 2 ? message({ id: 's' + i, from: 'wxid_x', text: 'hi' })
        : mediaMessage({ kind: 'image', id: 's' + i, from: 'wxid_x' }));
    const [, body] = await s.json('/events?after=0');
    const own = body.events.find(e => e.message_ref === 'owner-photo');
    assert.ok(own);
    assert.equal(body.events.filter(e => e.sender_ref === 'wxid_x').length, 50);
    assert.equal(s.bridge.live, 1);
    const [status, bytes] = await fetchMedia(s, own.attachments[0].media_id);
    assert.deepEqual([status, bytes.toString()], [200, 'PHOTO']);
  } finally { await s.bridge.close(); }
});

test('send-file: the plan follows what the fork can send: a bubble JPEG as an image, voice-bar PCM as a voice, the rest as a file', () => {
  const small = jpeg(MEDIA_SEND.imageMaxBytes), pcm16k = wav(32000, 3200);
  assert.deepEqual(MEDIA_SEND, { voiceSampleRate: 16000, voiceMaxMs: 60000, imageMaxBytes: 65536 });
  // An image bubble only for a real JPEG of at most 64 KiB, named .jpg when its name is not;
  // the file it falls back to never carries an image extension.
  assert.deepEqual(sendPlan({ kind: 'image', name: 'photo.jpeg', bytes: small }),
    [{ as: 'image', name: 'photo.jpeg' }, { as: 'file', name: 'photo.jpeg.bin' }]);
  assert.deepEqual(sendPlan({ kind: 'image', name: 'image-ab12.png', bytes: small }),
    [{ as: 'image', name: 'image-ab12.jpg' }, { as: 'file', name: 'image-ab12.png.bin' }]);
  assert.deepEqual(sendPlan({ kind: 'image', name: 'image-ab12.png', bytes: PNG_BYTES }), [{ as: 'file', name: 'image-ab12.png.bin' }]);
  assert.deepEqual(sendPlan({ kind: 'image', name: 'big.jpg', bytes: jpeg(MEDIA_SEND.imageMaxBytes + 1) }), [{ as: 'file', name: 'big.jpg.bin' }]);
  assert.deepEqual(sendPlan({ kind: 'image', name: 'chart', bytes: Buffer.from('GIF89a') }), [{ as: 'file', name: 'chart' }]);
  assert.ok(isBubbleJpeg(small) && !isBubbleJpeg(PNG_BYTES) && !isBubbleJpeg(Buffer.from([0xff, 0xd8])));
  // A voice bar for 16 kHz mono 16-bit PCM WAV of at most 60 s (the bridge encodes it at send
  // time); anything else - another rate, stereo, 8-bit, longer, not a WAV - is a file at once.
  const [voice, fallback] = sendPlan({ kind: 'voice', name: 'voice-ab12.wav', bytes: pcm16k });
  assert.deepEqual([voice.as, voice.pcm.equals(pcm16k.subarray(44)), fallback], ['voice', true, { as: 'file', name: 'voice-ab12.wav' }]);
  for (const bytes of [wav(48000, 4800), wav(64000, 6400, { channels: 2 }), wav(16000, 1600, { bits: 8 }),
    wav(32000, 32000 * 60 + 2), Buffer.from('OggS not a wav')])
    assert.deepEqual(sendPlan({ kind: 'voice', name: 'v.wav', bytes }), [{ as: 'file', name: 'v.wav' }]);
  assert.ok(voicePcm(wav(32000, 32000 * 60)), 'exactly 60 s is a voice bar');
  assert.deepEqual(sendPlan({ kind: 'file', name: '../a/报告.pdf', bytes: Buffer.from('%PDF') }), [{ as: 'file', name: '.._a_报告.pdf' }]);
  assert.deepEqual(sendPlan({ kind: 'file', name: 'chart.png', bytes: small }), [{ as: 'file', name: 'chart.png.bin' }]);
  for (const name of ['x.silk', 'x.SIL', 'x.slk', 'x.amr'])
    assert.deepEqual(sendPlan({ kind: 'file', name, bytes: Buffer.from('x') }), [{ as: 'file', name: name + '.bin' }]);
  // The fork's carrier() takes all after the last dot (the whole name when there is none) as the
  // extension: a name that is only an extension, with or without its dot, gets .bin too.
  assert.deepEqual(sendPlan({ kind: 'image', name: '.png', bytes: PNG_BYTES }), [{ as: 'file', name: '.png.bin' }]);
  for (const name of ['png', '.png', 'JPG', 'silk', '.silk', 'amr', '.amr'])
    assert.deepEqual(sendPlan({ kind: 'file', name, bytes: Buffer.from('x') }), [{ as: 'file', name: name + '.bin' }], name);
  assert.deepEqual(sendPlan({ kind: 'voice', name: '.amr', bytes: Buffer.from('#!AMR') }), [{ as: 'file', name: '.amr.bin' }]);
  assert.deepEqual(sendPlan({ kind: 'file', name: 'report', bytes: Buffer.from('x') }), [{ as: 'file', name: 'report' }]);
});

test('send-file: needs the token, sends through messageSendFile, and has its own 25 MiB limit (413, nothing sent)', async () => {
  const s = await mediaBridge({ mediaLimits: { ...MEDIA_LIMITS, sendBody: 1000 } });
  try {
    const query = { to: 'wxid_owner', kind: 'image', mime: 'image/jpeg', name: 'image-1.jpg' };
    const photo = jpeg(200);
    assert.deepEqual(await postFile(s, query, photo, null), [401, { error: 'unauthorized' }]);
    assert.deepEqual(await postFile(s, query, photo), [409, { error: 'not_logged_in' }]);
    const bot = await s.login();
    assert.deepEqual(await postFile(s, query, photo), [200, { ref: 'file-out-1', sent_as: 'image' }]);
    assert.deepEqual(bot.files, [{ to: 'wxid_owner', name: 'image-1.jpg', bytes: photo }]);
    assert.deepEqual(await postFile(s, query, Buffer.alloc(1001)), [413, { error: 'body_too_large' }]);
    assert.deepEqual(await postFile(s, { ...query, kind: 'video' }, Buffer.from('x')), [400, { error: 'bad_request' }]);
    assert.deepEqual(await postFile(s, { ...query, room: ROOM }, Buffer.from('x')), [400, { error: 'bad_request' }], 'to or room, not both');
    assert.deepEqual(await postFile(s, query, Buffer.alloc(0)), [400, { error: 'bad_request' }]);
    assert.deepEqual(await postFile(s, { ...query, to: 'wxid_unknown' }, Buffer.from('x')), [409, { error: 'recipient_unknown' }]);
    assert.equal(bot.files.length, 1, 'nothing else was sent');
    // POST /send keeps its own 64 KiB limit.
    assert.deepEqual(await s.json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'x'.repeat(64 * 1024) } }),
      [413, { error: 'body_too_large' }]);
  } finally { await s.bridge.close(); }
});

test('send-file: an image the fork cannot carry as a bubble goes as a file; a refused bubble goes again as a file', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const query = { to: 'wxid_owner', kind: 'image', mime: 'image/png', name: 'chart.png' };
    assert.deepEqual(await postFile(s, query, PNG_BYTES), [200, { ref: 'file-out-1', sent_as: 'file' }]);
    const big = jpeg(MEDIA_SEND.imageMaxBytes + 1);
    assert.deepEqual(await postFile(s, { ...query, mime: 'image/jpeg', name: 'big.jpg' }, big), [200, { ref: 'file-out-2', sent_as: 'file' }]);
    // The crate refuses a JPEG outside its small tier (say, wider than its thumbnail bound).
    bot.refuse = name => name.endsWith('.jpg');
    bot.refusal = 'prepare an image send: the thumbnail is larger than 320 px';
    const wide = jpeg(4000);
    assert.deepEqual(await postFile(s, { ...query, mime: 'image/jpeg', name: 'wide.jpg' }, wide), [200, { ref: 'file-out-3', sent_as: 'file' }]);
    assert.deepEqual(bot.files.map(f => [f.name, f.bytes.length]),
      [['chart.png.bin', PNG_BYTES.length], ['big.jpg.bin', big.length], ['wide.jpg.bin', wide.length]]);
  } finally { await s.bridge.close(); }
});

test('send-file: a voice goes as a voice bar - encodeVoice\'s SILK named <durationMs>.silk; refused at any step it goes as a file', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const audio = wav(32000, 32000 * 2);
    const query = { to: 'wxid_owner', kind: 'voice', mime: 'audio/wav', name: 'voice-ab12.wav' };
    assert.deepEqual(await postFile(s, query, audio), [200, { ref: 'file-out-1', sent_as: 'voice' }]);
    // The fork's encoder was given the PCM (not the WAV), and its note went out named by its length.
    assert.deepEqual(bot.encoded, [audio.subarray(44).toString('base64')]);
    assert.deepEqual([bot.files[0].name, bot.files[0].bytes.toString()], ['2000.silk', 'SILK:' + bot.encoded[0]]);
    const filed = () => bot.files.at(-1);
    // The puppet refuses the voice bar: the same WAV under its own name as a file.
    bot.refuse = name => name.endsWith('.silk');
    assert.deepEqual(await postFile(s, query, audio), [200, { ref: 'file-out-2', sent_as: 'file' }]);
    assert.deepEqual([filed().name, filed().bytes.equals(audio)], ['voice-ab12.wav', true]);
    bot.refuse = () => true;
    const [status, body] = await postFile(s, query, audio);
    assert.deepEqual([status, body.error], [502, 'send_failed'], 'both refused: a failed send');
    bot.refuse = () => false;
    // The encoder refuses, or answers a note no voice bar carries: a file.
    bot.encodeError = new Error('encode a voice note: Audio must be 16-bit samples');
    assert.deepEqual(await postFile(s, query, audio), [200, { ref: 'file-out-3', sent_as: 'file' }]);
    bot.encodeError = undefined;
    const encode = bot.puppet.session.encodeVoice;
    bot.puppet.session.encodeVoice = async pcm => ({ ...(await encode(pcm)), durationMs: 60_001 });
    assert.deepEqual(await postFile(s, query, audio), [200, { ref: 'file-out-4', sent_as: 'file' }]);
    // No live session to encode with: a file.
    delete bot.puppet.session;
    assert.deepEqual(await postFile(s, query, audio), [200, { ref: 'file-out-5', sent_as: 'file' }]);
    assert.deepEqual(bot.files.slice(2).map(f => f.name), ['voice-ab12.wav', 'voice-ab12.wav', 'voice-ab12.wav']);
    bot.puppet.session = { encodeVoice: encode };
    // Not 16 kHz mono 16-bit, longer than 60 s, or not a WAV: a file at once, the encoder never asked.
    const asked = bot.encoded.length;
    for (const [bytes, name] of [[wav(48000, 48000), 'v48k.wav'], [wav(64000, 64000, { channels: 2 }), 'stereo.wav'],
      [wav(32000, 32000 * 61), 'long.wav'], [Buffer.from('OggS not a wav'), 'voice-cd34.ogg']])
      assert.deepEqual((await postFile(s, { ...query, name }, bytes))[1].sent_as, 'file');
    assert.equal(bot.encoded.length, asked);
    assert.deepEqual(bot.files.slice(-4).map(f => f.name), ['v48k.wav', 'stereo.wav', 'long.wav', 'voice-cd34.ogg']);
  } finally { await s.bridge.close(); }
});

test('send-file: a media send whose outcome the fork calls uncertain is a failed send, never sent again as a file', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    bot.refuse = name => name.endsWith('.silk') || name.endsWith('.jpg');
    bot.refusal = 'WeChat media send outcome is uncertain';
    for (const [kind, name, bytes] of [['voice', 'v.wav', wav(32000, 3200)], ['image', 'p.jpg', jpeg(100)]]) {
      const [status, body] = await postFile(s, { to: 'wxid_owner', kind, mime: '', name }, bytes);
      assert.deepEqual([status, body.error, /uncertain/.test(body.detail)], [502, 'send_failed', true]);
    }
    assert.deepEqual(bot.files, []);
  } finally { await s.bridge.close(); }
});

test('send-file: a peer neo no longer knows is 409 recipient_unknown, and no fallback is tried', async () => {
  const s = await mediaBridge();
  try {
    const bot = await s.login();
    const tried = [];
    bot.puppet.messageSendFile = async (to, box) => { tried.push(box.name); throw new Error('Neo has no observed peer for wxid_owner'); };
    assert.deepEqual(await postFile(s, { to: 'wxid_owner', kind: 'voice', mime: 'audio/wav', name: 'v.wav' }, wav(32000, 3200)),
      [409, { error: 'recipient_unknown' }]);
    assert.deepEqual(tried, ['100.silk']);
  } finally { await s.bridge.close(); }
});

test('send-file: into a bound room through the group queue and its backstop', async () => {
  let clock = Date.parse('2026-11-21T10:00:00Z');
  const slept = [];
  const s = await mediaBridge({ now: () => new Date(clock), sleep: async ms => { slept.push(ms); clock += ms; },
    sendLimits: { sendGapMs: 10000, backstopPerHour: 2 },
    prepare: bot => { bot.Room = { load: id => ({ say: async box => ({ id: await bot.puppet.messageSendFile(id, box) }) }) }; } });
  try {
    const bot = await s.login();
    const query = { room: ROOM, kind: 'file', mime: 'application/pdf', name: 'a.pdf' };
    assert.deepEqual(await postFile(s, query, Buffer.from('%PDF')), [409, { error: 'room_not_bound' }]);
    await withRooms(s);
    const answers = await Promise.all([1, 2, 3].map(() => postFile(s, query, Buffer.from('%PDF'))));
    assert.deepEqual(answers.map(([status]) => status), [200, 200, 429]);
    assert.deepEqual(bot.files.map(f => [f.to, f.name]), [[ROOM, 'a.pdf'], [ROOM, 'a.pdf']]);
    assert.deepEqual(slept, [10000]);
  } finally { await s.bridge.close(); }
});

test('run.sh gives /tmp a 32 MiB tmpfs (one fetched media file at a time)', () => {
  const script = readFileSync(fileURLToPath(new URL('./run.sh', import.meta.url)), 'utf8');
  assert.match(script, /--tmpfs \/tmp:rw,noexec,nosuid,size=32m /);
});

// --- step 19.1: the saved login (design-steward 7.2.1) -----------------------------------------
/** The saved login as main.mjs hands it in, faked: `present` and the write counts are what the
 *  fork's savedLoginStatus() answers (`off` set: its configuration error, present false);
 *  `account` is the bridge's own record of the last QR-login account (null: none). The api has
 *  no way to delete anything: the bridge never deletes a saved login, only the fork does. */
function fakeSaved({ present = true, off, account = 'wxid_bot', writes = 0, writeFailures = 0 } = {}) {
  const saved = { present, off, account, writes, writeFailures, recorded: [], recordError: undefined, statusCalls: 0 };
  saved.api = Object.freeze({
    status: () => {
      saved.statusCalls++;
      return saved.off ? { present: false, off: saved.off, writes: 0, write_failures: 0 }
        : { present: saved.present, writes: saved.writes, write_failures: saved.writeFailures };
    },
    readAccount: () => saved.account ?? undefined,
    recordAccount: id => { if (saved.recordError) throw saved.recordError; saved.recorded.push(id); saved.account = id; },
  });
  return saved;
}
// A test that hangs fails at this bound instead of holding up the suite.
const LIMIT = { timeout: 15_000 };
const FAST = Object.freeze({ waitMs: 200, retryMs: Object.freeze([30, 60, 90]), holdGroupMs: 150, countLogMs: 40 });
const RESUME = Object.freeze({ resume: true, expectUsername: 'wxid_bot' });
const resumed = async (options = {}) => {
  const saved = options.saved ?? fakeSaved();
  const s = await started({ savedLogin: saved.api, resumeLimits: FAST, ...options });
  return { ...s, saved };
};
const events = async s => (await s.json('/events?after=0'))[1].events;
const sleepMs = ms => new Promise(resolve => setTimeout(resolve, ms));

test('saved login: the limits are code constants, the resume bound strictly above the fork\'s ~70 s worst case', LIMIT, () => {
  assert.deepEqual({ ...RESUME_LIMITS, retryMs: [...RESUME_LIMITS.retryMs] },
    { waitMs: 150_000, retryMs: [30_000, 120_000, 600_000], holdGroupMs: 120_000, countLogMs: HOUR });
  assert.ok(Object.isFrozen(RESUME_LIMITS) && Object.isFrozen(RESUME_LIMITS.retryMs));
  assert.ok(RESUME_LIMITS.waitMs > 70_000);
});

test('saved login: none to resume starts idle (boot) and builds no bot; one to resume goes resuming -> logged_in with no QR, expecting the recorded account', LIMIT, async () => {
  const none = await resumed({ saved: fakeSaved({ present: false }) });
  try {
    const [, body] = await none.json('/status');
    assert.deepEqual([body.phase, body.idle_reason, body.resumable], ['idle', 'boot', false]);
    await sleepMs(50);
    assert.deepEqual(none.bots, [], 'nothing to resume: WeChat is asked for nothing');
  } finally { await none.bridge.close(); }

  const logs = [];
  const s = await resumed({ log: line => logs.push(line) });
  try {
    let [, body] = await s.json('/status');
    assert.deepEqual([body.phase, body.logged_in, body.resumable, typeof body.since], ['resuming', false, true, 'string']);
    assert.equal(s.bots.length, 1);
    assert.deepEqual(s.bot().options, RESUME, 'the account the last QR login was with is handed to the fork');
    await s.bridge.settled();
    assert.deepEqual(s.bot().calls, ['start']);
    s.bot().emit('login', { id: 'wxid_bot' });
    [, body] = await s.json('/status');
    assert.deepEqual([body.phase, body.logged_in, body.user_id, body.qr, body.qr_svg, body.resumable],
      ['logged_in', true, 'wxid_bot', undefined, undefined, true]);
    assert.ok(logs.includes('logged in (the saved login resumed)'));
    assert.deepEqual(s.saved.recorded, [], 'a resumed login does not rewrite the record');
  } finally { await s.bridge.close(); }
});

test('saved login: with no account recorded (the first start after the upgrade) the resume expects none and is taken as it comes', LIMIT, async () => {
  const s = await resumed({ saved: fakeSaved({ account: null }) });
  try {
    assert.deepEqual(s.bot().options, { resume: true }, 'no expectUsername: the fork compares nothing');
    await s.bridge.settled();
    s.bot().emit('login', { id: 'wxid_whoever' });
    const [, body] = await s.json('/status');
    assert.deepEqual([body.phase, body.user_id, body.resumable], ['logged_in', 'wxid_whoever', true]);
    assert.deepEqual(s.saved.recorded, [], 'only a QR login is recorded');
  } finally { await s.bridge.close(); }
});

test('saved login: refused, unreadable, throttled and none each leave their idle reason, with no retry and no QR', LIMIT, async () => {
  const cases = [
    ['RESUME_REJECTED', 'resume_rejected', false],
    ['RESUME_UNREADABLE', 'resume_unreadable', true],
    ['RESUME_THROTTLED', 'resume_throttled', true],
    ['RESUME_NONE', 'boot', false],
  ];
  for (const [prefix, reason, present] of cases) {
    const s = await resumed();
    try {
      await s.bridge.settled();
      // The fork deleted a refused file (and found none for RESUME_NONE) before it answered.
      s.saved.present = present;
      s.bot().emit('error', new Error(`${prefix}: the saved login was not resumed`));
      s.bot().emit('logout', { id: 'wxid_bot' });
      await s.bridge.settled();
      const [, body] = await s.json('/status');
      assert.deepEqual([body.phase, body.idle_reason, body.resumable], ['idle', reason, present], prefix);
      assert.deepEqual(s.bot().calls, ['start', 'stop'], prefix);
      await sleepMs(250);
      assert.equal(s.bots.length, 1, `${prefix}: never tried again, never a QR by itself`);
    } finally { await s.bridge.close(); }
  }
});

test('saved login: the fork\'s word is read from the error\'s message and from its data, whichever carries it', LIMIT, async () => {
  for (const [shape, reason] of [['data', 'resume_throttled'], ['message', 'resume_throttled'], ['string', 'resume_throttled'],
    ['data-mismatch', 'resume_account_mismatch']]) {
    const s = await resumed();
    try {
      await s.bridge.settled();
      const word = shape === 'data-mismatch' ? 'RESUME_MISMATCH' : 'RESUME_THROTTLED';
      let error;
      if (shape === 'string') error = `${word}: cap reached`;
      else if (shape === 'message') error = new Error(`${word}: cap reached`);
      else { error = new Error('Neo serve failed'); error.data = `${word}: cap reached`; }
      s.bot().emit('error', error);
      assert.deepEqual([s.bridge.state.phase, s.bridge.state.idle_reason], ['idle', reason], shape);
      await sleepMs(150);
      assert.equal(s.bots.length, 1, `${shape}: not retried`);
    } finally { await s.bridge.close(); }
  }
});

test('saved login: a passing failure (network, busy, a start that throws, no answer in time, a logout with no error) is idle resume_failed and is tried again', LIMIT, async () => {
  const cases = [
    ['network', bot => bot.emit('error', new Error('Neo could not resume the saved login: timed out'))],
    ['busy', bot => bot.emit('error', new Error('RESUME_BUSY: another resume is running'))],
    ['start', undefined],
    ['silence', () => {}],
    ['logout', bot => bot.emit('logout', { id: 'wxid_bot' })],
  ];
  for (const [name, happen] of cases) {
    const s = await resumed({ prepare: (bot, opts) => { if (name === 'start' && opts?.resume && !bot.done) bot.startError = new Error('no session'); } });
    try {
      await s.bridge.settled();
      happen?.(s.bots[0]);
      await until(() => s.bridge.state.idle_reason === 'resume_failed' || s.bots.length > 1, 1000);
      if (s.bots.length === 1) assert.deepEqual([s.bridge.state.phase, s.bridge.state.idle_reason], ['idle', 'resume_failed'], name);
      await until(() => s.bots.length === 2, 1000);
      assert.deepEqual(s.bots[1].options, RESUME, name);
      assert.ok(s.bots[0].calls.includes('stop'), `${name}: the first bot was stopped`);
    } finally { await s.bridge.close(); }
  }
});

test('saved login: retries at retryMs (3), each on a new bot after the last one stopped, then idle resume_failed for good', LIMIT, async () => {
  const order = [];
  const s = await resumed({ prepare: (bot, opts) => {
    const index = order.filter(line => line.endsWith(':built')).length;
    order.push(`${index}:built`);
    bot.startError = new Error('Neo could not resume the saved login: network');
    // The first bot stops slowly: the first retry (after 30 ms) must wait for it.
    const stop = bot.stop;
    bot.stop = async () => { if (index === 0) await sleepMs(150); await stop(); order.push(`${index}:stopped`); };
    assert.deepEqual(opts, RESUME);
  } });
  try {
    await until(() => s.bots.length === 4 && s.bridge.state.idle_reason === 'resume_failed' && !s.bridge.state.logged_in, 2000);
    await s.bridge.settled();
    await sleepMs(300);
    assert.equal(s.bots.length, 4, 'one resume and three retries, no more');
    assert.deepEqual([s.bridge.state.phase, s.bridge.state.idle_reason], ['idle', 'resume_failed']);
    assert.ok(order.indexOf('0:stopped') < order.indexOf('1:built'), order.join(' '));
    for (const bot of s.bots) assert.deepEqual(bot.calls, ['start', 'stop']);
  } finally { await s.bridge.close(); }
});

test('saved login: POST /login is refused 409 resuming while resuming, taken in a resume_* idle, and it voids a pending retry', LIMIT, async () => {
  const s = await resumed({ resumeLimits: { ...FAST, retryMs: [150, 150, 150] }, loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
  try {
    assert.deepEqual(await s.json('/login', { method: 'POST' }), [409, { refused: 'resuming' }]);
    assert.equal(s.bots.length, 1);
    await s.bridge.settled();
    s.bot().emit('error', new Error('Neo could not resume the saved login: offline'));
    assert.equal(s.bridge.state.idle_reason, 'resume_failed');
    assert.equal((await s.request())[0], 202);
    assert.equal(s.bots.length, 2);
    assert.equal(s.bots[1].options, undefined, 'a QR login is built as before');
    await sleepMs(300);
    assert.equal(s.bots.length, 2, 'the pending retry did nothing');
    assert.equal(s.bridge.state.phase, 'starting');
  } finally { await s.bridge.close(); }
});

test('saved login: a retry already waiting for the last bot\'s slow stop does not start once a QR login was accepted meanwhile', LIMIT, async () => {
  const s = await resumed({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 }, prepare: (bot, opts) => {
    if (opts?.resume) { const stop = bot.stop; bot.stop = async () => { await sleepMs(300); await stop(); }; }
  } });
  try {
    await s.bridge.settled();
    s.bot().emit('error', new Error('Neo could not resume the saved login: offline'));
    assert.equal(s.bridge.state.idle_reason, 'resume_failed');
    await sleepMs(80); // the 30 ms retry now waits in settle() for the slow stop
    assert.equal((await s.json('/login', { method: 'POST' }))[0], 202);
    await sleepMs(500);
    assert.equal(s.bots.length, 2, 'only the QR bot was added');
    assert.equal(s.bots[1].options, undefined);
    assert.equal(s.bridge.state.phase, 'starting', 'no resume runs beside the QR login');
  } finally { await s.bridge.close(); }
});

test('saved login: an ended resume\'s bot changes nothing later - its login, error or logout', LIMIT, async () => {
  const s = await resumed();
  try {
    await s.bridge.settled();
    const first = s.bots[0];
    await until(() => s.bridge.state.idle_reason === 'resume_failed', 1000);
    await until(() => s.bots.length === 2 && s.bridge.state.phase === 'resuming', 1000);
    first.emit('login', { id: 'wxid_bot' });
    first.emit('error', new Error('RESUME_REJECTED'));
    first.emit('logout', { id: 'wxid_bot' });
    assert.equal(s.bridge.state.phase, 'resuming');
    s.bots[1].emit('login', { id: 'wxid_bot' });
    assert.equal(s.bridge.state.phase, 'logged_in');
  } finally { await s.bridge.close(); }
});

test('saved login: RESUME_MISMATCH (the fork found another account and deleted it) is idle resume_account_mismatch, not resumable, nothing sent, no retry, no QR', LIMIT, async () => {
  const s = await resumed();
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('message', message({ text: 'not for anyone' }));
    bot.emit('error', new Error('RESUME_MISMATCH: the saved login is another account'));
    bot.emit('logout', { id: 'wxid_bot' });
    await s.bridge.settled();
    // Even if the fork's deletion failed and the file is still there, it is not resumable here.
    assert.equal(s.saved.present, true);
    const [, body] = await s.json('/status');
    assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.resumable], ['idle', 'resume_account_mismatch', false, false]);
    assert.deepEqual(bot.calls, ['start', 'stop']);
    assert.deepEqual(bot.said, []);
    assert.deepEqual(await s.json('/send', { method: 'POST', body: { to: 'wxid_owner', text: 'x' } }), [409, { error: 'not_logged_in' }]);
    await s.json('/rooms', { method: 'PUT', body: { owner_ref: 'wxid_owner', rooms: [] } });
    assert.deepEqual(await events(s), [], 'what it held is dropped, not released');
    await sleepMs(250);
    assert.equal(s.bots.length, 1, 'no retry and no QR by itself');
    assert.deepEqual(s.saved.recorded, []);
  } finally { await s.bridge.close(); }
});

test('saved login: a resumed login that still comes back as another account than expected is not taken (idle resume_account_mismatch, never logged)', LIMIT, async () => {
  const logs = [];
  const s = await resumed({ log: line => logs.push(line) });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_other' });
    bot.emit('message', message({ text: 'not for anyone' }));
    await s.bridge.settled();
    const [, body] = await s.json('/status');
    assert.deepEqual([body.phase, body.idle_reason, body.logged_in, body.user_id, body.resumable],
      ['idle', 'resume_account_mismatch', false, undefined, false]);
    assert.deepEqual(bot.calls, ['start', 'stop']);
    await s.json('/rooms', { method: 'PUT', body: { owner_ref: 'wxid_owner', rooms: [] } });
    assert.deepEqual(await events(s), []);
    assert.ok(!logs.some(line => line.includes('wxid_other')), 'the account is never logged');
    await sleepMs(250);
    assert.equal(s.bots.length, 1);
  } finally { await s.bridge.close(); }
});

test('saved login: after a mismatch, a QR login (once the fork has written its saved login) is resumable again, and a later local drop resumes it', LIMIT, async () => {
  const s = await resumed({ loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
  try {
    await s.bridge.settled();
    s.saved.present = false; // the fork deleted the other account's file
    s.bot().emit('error', new Error('RESUME_MISMATCH'));
    await s.bridge.settled();
    assert.equal((await s.json('/status'))[1].resumable, false);
    const qr = await s.login();
    assert.deepEqual(s.saved.recorded, ['wxid_bot']);
    assert.equal((await s.json('/status'))[1].resumable, false, 'nothing written for this login yet');
    s.saved.present = true;
    s.saved.writes++;
    assert.equal((await s.json('/status'))[1].resumable, true);
    qr.emit('logout', { id: 'wxid_bot' });
    await until(() => s.bots.length === 3 && s.bridge.state.phase === 'resuming', 1000);
    assert.deepEqual(s.bots[2].options, RESUME);
  } finally { await s.bridge.close(); }
});

test('saved login: a QR login records its account; resumable only once the fork has written since its QR was asked for', LIMIT, async () => {
  const s = await resumed({ saved: fakeSaved({ present: false, account: null, writes: 3 }) });
  try {
    await s.login();
    assert.deepEqual(s.saved.recorded, ['wxid_bot']);
    // The fork's first write failed: an older file (here: another account's) is still there.
    s.saved.present = true;
    s.saved.writeFailures++;
    let [, body] = await s.json('/status');
    assert.deepEqual([body.resumable, body.writes, body.write_failures], [false, 3, 1], 'never optimistic');
    // A later write (a cookie update) succeeds: the file on disk is now this login's.
    s.saved.writes++;
    [, body] = await s.json('/status');
    assert.deepEqual([body.resumable, body.writes, body.write_failures], [true, 4, 1]);
    // A local drop now resumes this account.
    s.bot().emit('logout', { id: 'wxid_bot' });
    await until(() => s.bots.length === 2 && s.bridge.state.phase === 'resuming', 1000);
    assert.deepEqual(s.bots[1].options, RESUME);
  } finally { await s.bridge.close(); }

  // The first write failed and the login drops here: no resume of the older file.
  const d = await resumed({ saved: fakeSaved({ present: true, account: 'wxid_old' }), loginLimits: { ...LOGIN_LIMITS, minGapMs: 0 } });
  try {
    await d.bridge.settled();
    d.bot().emit('error', new Error('RESUME_UNREADABLE'));
    const qr = await d.login();
    d.saved.writeFailures++;
    assert.equal((await d.json('/status'))[1].resumable, false);
    qr.emit('logout', { id: 'wxid_bot' });
    await d.bridge.settled();
    await sleepMs(150);
    assert.deepEqual([d.bots.length, d.bridge.state.phase, d.bridge.state.idle_reason], [2, 'idle', 'logged_out']);
  } finally { await d.bridge.close(); }

  const logs = [];
  const failing = fakeSaved({ present: false, account: 'wxid_old' });
  failing.recordError = new Error('ENOSPC');
  const f = await resumed({ saved: failing, log: line => logs.push(line) });
  try {
    await f.login();
    failing.present = true;
    failing.writes++;
    const [, body] = await f.json('/status');
    assert.deepEqual([body.phase, body.resumable], ['logged_in', false], 'the record on disk still names another account');
    assert.ok(logs.some(line => line.startsWith('the account logged in with could not be recorded') && line.endsWith('ENOSPC')));
  } finally { await f.bridge.close(); }
});

test('saved login: /status carries the fork\'s write counts; write failures never read as off; off shows no counts and logs its reason only', LIMIT, async () => {
  const logs = [];
  const saved = fakeSaved({ writes: 4, writeFailures: 2 });
  const s = await resumed({ saved, log: line => logs.push(line) });
  try {
    await s.bridge.settled();
    s.bot().emit('login', { id: 'wxid_bot' });
    let [, body] = await s.json('/status');
    assert.deepEqual([body.resumable, body.writes, body.write_failures], [true, 4, 2]);
    await until(() => logs.includes('saved login: 4 writes, 2 write failures'), 1000);
    await sleepMs(150);
    assert.equal(logs.filter(line => line.startsWith('saved login: ')).length, 1, 'unchanged counts are not logged again');
    saved.writes = 5;
    await until(() => logs.includes('saved login: 5 writes, 2 write failures'), 1000);
    saved.writeFailures = 9;
    [, body] = await s.json('/status');
    assert.deepEqual([body.resumable, body.writes, body.write_failures], [true, 5, 9]);
    assert.ok(!logs.some(line => line.includes('off')), logs.join('\n'));
  } finally { await s.bridge.close(); }

  for (const off of ['key', 'dir', 'not_configured']) {
    const offLogs = [];
    const o = await resumed({ saved: fakeSaved({ off }), log: line => offLogs.push(line) });
    try {
      assert.ok(offLogs.includes(`saved login off (${off})`), off);
      const [, body] = await o.json('/status');
      assert.equal(body.resumable, false);
      assert.ok(!('writes' in body) && !('write_failures' in body), `${off}: no write counts`);
      await sleepMs(100);
      assert.ok(!offLogs.some(line => line.startsWith('saved login: ')), `${off}: no write counts logged`);
      assert.deepEqual(o.bots, []);
    } finally { await o.bridge.close(); }
  }
});

test('saved login off (WECHAT_BRIDGE_SAVE_LOGIN=0) and no saved-login calls at all are the bridge as it was before 19.1', LIMIT, async () => {
  // One script of today's behaviour: a QR login, a 1:1 message before any PUT /rooms (relayed
  // at once: no hold), the account never recorded, a logout that resumes nothing.
  const trace = async savedLogin => {
    const saved = savedLogin;
    const s = await started({ savedLogin: saved?.api, resumeLimits: FAST });
    try {
      const out = [];
      const status = async () => { const [, b] = await s.json('/status'); delete b.since; out.push(b); };
      await status();
      const bot = await s.login();
      out.push(s.bots.map(b => b.options));
      bot.emit('message', message({ id: 'a1', text: 'before any PUT' }));
      bot.emit('message', roomMessage({ id: 'g1', text: '@管家 hi' }));
      out.push((await events(s)).map(e => [e.type, e.message_ref]));
      await status();
      bot.emit('logout', { id: 'wxid_bot' });
      await s.bridge.settled();
      await sleepMs(100);
      await status();
      out.push(s.bots.length, saved?.recorded ?? []);
      return out;
    } finally { await s.bridge.close(); }
  };
  const today = await trace(undefined);
  assert.deepEqual(today[0], { phase: 'idle', idle_reason: 'boot', logged_in: false, resumable: false });
  assert.deepEqual(today[2], [['message', 'a1']]);
  const off = await trace(fakeSaved({ off: 'not_configured' }));
  assert.deepEqual(off, today);
  // With login disabled (the image's start check) a saved login is never resumed either.
  const skip = await resumed({ loginEnabled: false });
  try {
    assert.deepEqual(skip.bots, []);
    assert.equal(skip.bridge.state.phase, 'idle');
  } finally { await skip.bridge.close(); }
});

test('saved login: a login that ends here with its saved login still on disk is resumed; one WeChat ended (file gone) is not', LIMIT, async () => {
  const s = await resumed();
  try {
    await s.bridge.settled();
    s.bot().emit('login', { id: 'wxid_bot' });
    s.bot().emit('logout', { id: 'wxid_bot' });
    assert.equal(s.bridge.state.idle_reason, 'logged_out');
    await until(() => s.bots.length === 2 && s.bridge.state.phase === 'resuming', 1000);
    assert.deepEqual(s.bots[1].options, RESUME);
    assert.deepEqual(s.bots[0].calls, ['start', 'stop']);
    s.bots[1].emit('login', { id: 'wxid_bot' });
    // WeChat ends it: the fork deletes the saved login before the puppet reports the logout.
    s.saved.present = false;
    s.bots[1].emit('logout', { id: 'wxid_bot' });
    await s.bridge.settled();
    await sleepMs(100);
    assert.deepEqual([s.bots.length, s.bridge.state.phase, s.bridge.state.idle_reason], [2, 'idle', 'logged_out']);
  } finally { await s.bridge.close(); }
});

test('saved login: a media download that fails (400, Err) never resumes, asks for a QR or changes resumable', LIMIT, async () => {
  const tmp = mkdtempSync(join(tmpdir(), 'wechat-bridge-media-'));
  const s = await resumed({ tmpDir: tmp, makeFileBox, fetchLimits: { ...MEDIA_FETCH, attempts: 2 } });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_bot' });
    await withRooms(s);
    const photo = mediaMessage({ kind: 'image', id: 'p400', fail: 'download failed: HTTP 400 Err' });
    bot.emit('message', photo);
    const id = (await events(s)).at(-1).attachments[0].media_id;
    const before = (await s.json('/status'))[1];
    assert.deepEqual(await s.json('/media/' + id), [503, { error: 'unavailable' }]);
    // Wechaty reports a failed download as an error event too.
    bot.emit('error', new Error('download failed: HTTP 400 Err'));
    assert.deepEqual(await s.json('/media/' + id), [410, { error: 'gone' }]);
    await s.bridge.settled();
    await sleepMs(150);
    const after = (await s.json('/status'))[1];
    assert.deepEqual([after.phase, after.logged_in, after.resumable, after.qr], ['logged_in', true, before.resumable, undefined]);
    assert.equal(before.resumable, true);
    assert.equal(s.bots.length, 1, 'no resume and no QR bot');
    assert.deepEqual(bot.calls, ['start'], 'the live bot is never stopped');
    assert.equal(photo.fetched.count, 2);
  } finally { await s.bridge.close(); }
});

test('saved login: until the first PUT /rooms what arrives is held, then released through the usual rules', LIMIT, async () => {
  const s = await resumed({ resumeLimits: { ...FAST, holdGroupMs: 60_000 } });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('message', message({ id: 'o1', text: 'owner first' }));
    bot.emit('message', message({ id: 'x1', from: 'wxid_stranger', text: 'stranger' }));
    bot.emit('message', roomMessage({ id: 'g1', text: '@管家 bound room' }));
    bot.emit('message', roomMessage({ id: 'u1', roomId: 'other@chatroom', text: 'unbound room' }));
    assert.deepEqual(await events(s), [], 'nothing before the host has named the account and rooms');
    await withRooms(s);
    const out = await events(s);
    assert.deepEqual(out.map(e => [e.type, e.message_ref]), [['message', 'o1'], ['message', 'x1'], ['group_message', 'g1']]);
    bot.emit('message', message({ id: 'o2', text: 'after the PUT' }));
    assert.equal((await events(s)).at(-1).message_ref, 'o2', 'no hold after the first PUT');
  } finally { await s.bridge.close(); }
});

test('saved login: past holdGroupMs group messages pass the rules (no room bound: dropped), 1:1 ones stay held to the first PUT', LIMIT, async () => {
  const s = await resumed({ ringSize: 500, strangerRingSize: 1 });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('message', roomMessage({ id: 'g1', text: '@管家 early' }));
    bot.emit('message', message({ id: 'x1', from: 'wxid_stranger', text: 'stranger early' }));
    await sleepMs(250);
    bot.emit('message', roomMessage({ id: 'g2', text: '@管家 late' }));
    bot.emit('message', message({ id: 'o1', text: 'owner' }));
    assert.deepEqual(await events(s), [], 'groups dropped, 1:1 still held');
    await withRooms(s);
    const out = await events(s);
    assert.deepEqual(out.map(e => e.message_ref), ['x1', 'o1'], 'the group messages are gone, not stored');
    // The stranger's message was held until owner_ref was known: it is in the stranger ring
    // (size 1 here), so the next stranger pushes it out and the owner's stays.
    bot.emit('message', message({ id: 'x2', from: 'wxid_stranger', text: 'stranger again' }));
    assert.deepEqual((await events(s)).map(e => e.message_ref), ['o1', 'x2']);
  } finally { await s.bridge.close(); }
});

test('saved login: what a login held is dropped when that login ends; each kind is held at most its ring\'s size', LIMIT, async () => {
  const s = await resumed({ ringSize: 2, groupRingSize: 1, resumeLimits: { ...FAST, holdGroupMs: 60_000 } });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('message', mediaMessage({ kind: 'image', id: 'p1' }));
    s.saved.present = false;
    bot.emit('logout', { id: 'wxid_bot' });
    await withRooms(s);
    assert.deepEqual(await events(s), []);
    assert.equal(s.bridge.live, 0, 'no media id for a message of an ended login');
  } finally { await s.bridge.close(); }

  const c = await resumed({ ringSize: 2, groupRingSize: 1, resumeLimits: { ...FAST, holdGroupMs: 60_000 } });
  try {
    await c.bridge.settled();
    const bot = c.bot();
    bot.emit('login', { id: 'wxid_bot' });
    // The hold's own bound, not the rings': two 1:1 messages and one group message at most, so
    // the owner's o1 and the bound room's g1 are dropped as later ones (that the rings would
    // not have pushed them out for) arrive.
    bot.emit('message', message({ id: 'o1' }));
    bot.emit('message', message({ id: 'x1', from: 'wxid_stranger' }));
    bot.emit('message', message({ id: 'x2', from: 'wxid_stranger' }));
    bot.emit('message', roomMessage({ id: 'g1', text: '@管家 hi' }));
    bot.emit('message', roomMessage({ id: 'u1', roomId: 'other@chatroom', text: 'unbound' }));
    await withRooms(c);
    assert.deepEqual((await events(c)).map(e => e.message_ref), ['x1', 'x2']);
  } finally { await c.bridge.close(); }
});

test('saved login: a group flood larger than the 1:1 hold never pushes the held 1:1 messages out (each kind has its own bound)', LIMIT, async () => {
  const s = await resumed({ ringSize: 3, groupRingSize: 50, resumeLimits: { ...FAST, holdGroupMs: 60_000 } });
  try {
    await s.bridge.settled();
    const bot = s.bot();
    bot.emit('login', { id: 'wxid_bot' });
    bot.emit('message', message({ id: 'o1', text: 'owner 1' }));
    for (let i = 0; i < 20; i++) bot.emit('message', roomMessage({ id: `g${i}`, text: `@管家 flood ${i}` }));
    bot.emit('message', message({ id: 'o2', text: 'owner 2' }));
    bot.emit('message', message({ id: 'o3', text: 'owner 3' }));
    await withRooms(s);
    const out = (await events(s)).map(e => e.message_ref);
    assert.deepEqual(out.filter(ref => ref.startsWith('o')), ['o1', 'o2', 'o3'], out.join(' '));
    assert.equal(out.length, 23);
  } finally { await s.bridge.close(); }
});

test('saved login: the account record is written atomically (0600, no temporary left) and read back; anything odd reads as none', LIMIT, () => {
  const dir = mkdtempSync(join(tmpdir(), 'wechat-bridge-account-'));
  const record = accountRecord(dir);
  assert.equal(record.readAccount(), undefined);
  record.recordAccount('wxid_bot');
  assert.equal(record.readAccount(), 'wxid_bot');
  assert.equal(statSync(join(dir, ACCOUNT_FILE)).mode & 0o777, 0o600);
  assert.deepEqual(readdirSync(dir), [ACCOUNT_FILE]);
  record.recordAccount('wxid_new');
  assert.equal(record.readAccount(), 'wxid_new');
  writeFileSync(join(dir, ACCOUNT_FILE), 'not json');
  assert.equal(record.readAccount(), undefined);
  writeFileSync(join(dir, ACCOUNT_FILE), JSON.stringify({ v: 2, user_id: 'wxid_bot' }));
  assert.equal(record.readAccount(), undefined);
  const other = mkdtempSync(join(tmpdir(), 'wechat-bridge-account-'));
  writeFileSync(join(other, 'target'), JSON.stringify({ v: 1, user_id: 'wxid_bot' }));
  const linked = mkdtempSync(join(tmpdir(), 'wechat-bridge-account-'));
  symlinkSync(join(other, 'target'), join(linked, ACCOUNT_FILE));
  assert.equal(accountRecord(linked).readAccount(), undefined, 'a link is never followed');
  assert.equal(accountRecord(undefined).readAccount(), undefined);
  assert.throws(() => accountRecord(undefined).recordAccount('wxid_bot'));
  assert.throws(() => accountRecord(join(dir, 'missing')).recordAccount('wxid_bot'));
});

test('the bridge and main.mjs never delete a saved-login file: no delete call reaches the fork, no account.v1 path is named', LIMIT, () => {
  const bridgeSource = readFileSync(new URL('./bridge.mjs', import.meta.url), 'utf8');
  const mainSource = readFileSync(new URL('./main.mjs', import.meta.url), 'utf8');
  for (const [name, source] of [['bridge.mjs', bridgeSource], ['main.mjs', mainSource]]) {
    assert.doesNotMatch(source, /forgetSavedLogin|deleteSavedLogin|account\.v1|PUPPET_NEO_STATE_DIR\)/, name);
    assert.doesNotMatch(source, /\bforget\s*\(/, name);
  }
  // main.mjs records the account in the bridge's own directory, never the fork's.
  assert.match(mainSource, /accountRecord\(process\.env\.WECHAT_BRIDGE_ACCOUNT_DIR\)/);
});

test('run.sh: the saved login\'s key (0600, read-only file mount), state (0700, read-write directory mount) and the bridge\'s account record (its own directory, outside data/), outside ~/.aitelier-secrets', LIMIT, () => {
  const home = mkdtempSync(join(tmpdir(), 'wechat-bridge-save-'));
  mkdirSync(join(home, '.aitelier-secrets'));
  writeFileSync(join(home, '.aitelier-secrets', 'wechat-bridge-token'), 'throwaway\n');
  mkdirSync(join(home, 'bin'));
  writeFileSync(join(home, 'bin', 'docker'), '#!/bin/sh\nprintf "%s\\n" "$@"\n');
  chmodSync(join(home, 'bin', 'docker'), 0o755);
  const script = fileURLToPath(new URL('./run.sh', import.meta.url));
  const run = extra => execFileSync('sh', [script], { encoding: 'utf8',
    env: { PATH: `${join(home, 'bin')}:/usr/bin:/bin`, HOME: home, ...extra } }).split('\n');
  const after = (args, flag) => args.filter((arg, i) => args[i - 1] === flag);
  const state = join(home, '.local/share/aitelier/wechat-bridge-state');
  const key = join(home, '.local/share/aitelier/wechat-bridge-key');
  const account = join(home, '.local/share/aitelier/wechat-bridge-account');
  // A state directory left open by hand is closed again.
  mkdirSync(state, { recursive: true });
  chmodSync(state, 0o755);
  const args = run({});
  assert.deepEqual(after(args, '--volume'), [`${home}/.aitelier-secrets/wechat-bridge-token:/home/bridge/.aitelier-secrets/wechat-bridge-token:ro`,
    `${key}/key:/home/bridge/saved-login-key:ro`, `${state}/data:/home/bridge/state:rw`, `${account}:/home/bridge/account:rw`]);
  assert.deepEqual(after(args, '--env'), ['PUPPET_NEO_STATE_DIR=/home/bridge/state', 'PUPPET_NEO_STATE_KEY_FILE=/home/bridge/saved-login-key',
    'WECHAT_BRIDGE_ACCOUNT_DIR=/home/bridge/account']);
  assert.equal(args.at(-2), 'aitelier-wechat-bridge:local', 'the mounts come before the image');
  const keyBytes = readFileSync(join(key, 'key'));
  assert.equal(keyBytes.length, 32);
  assert.equal(statSync(join(key, 'key')).mode & 0o777, 0o600);
  for (const dir of [state, join(state, 'data'), key, account]) assert.equal(statSync(dir).mode & 0o777, 0o700, dir);
  assert.ok(!readdirSync(key).some(name => name !== 'key'), 'no temporary key left');
  for (const path of after(args, '--volume').slice(1)) assert.ok(!path.includes('.aitelier-secrets'), path);
  assert.ok(!account.startsWith(state), 'the record is not inside the saved login\'s directory');
  // A second start keeps the key: it is made once.
  run({});
  assert.deepEqual(readFileSync(join(key, 'key')), keyBytes);
  // The off switch (run.sh, before the container starts - never the bridge): neither mount
  // nor variable, and the saved login and record deleted (never read); the resume times and
  // the key stay.
  const data = join(state, 'data');
  for (const name of ['account.v1', 'account.v1.tmp-0011223344556677', 'resumes']) writeFileSync(join(data, name), 'x');
  for (const name of ['account.user', 'account.user.tmp-aa']) writeFileSync(join(account, name), 'x');
  const off = run({ WECHAT_BRIDGE_SAVE_LOGIN: '0' });
  assert.deepEqual(after(off, '--volume'), [`${home}/.aitelier-secrets/wechat-bridge-token:/home/bridge/.aitelier-secrets/wechat-bridge-token:ro`]);
  assert.deepEqual(after(off, '--env'), []);
  assert.ok(!off.some(arg => arg.includes('PUPPET_NEO') || arg.includes('WECHAT_BRIDGE_ACCOUNT_DIR')));
  assert.deepEqual(readdirSync(data), ['resumes']);
  assert.deepEqual(readdirSync(account), []);
  assert.ok(existsSync(join(key, 'key')));
  // Off with nothing on disk at all still starts.
  const empty = mkdtempSync(join(tmpdir(), 'wechat-bridge-empty-'));
  mkdirSync(join(empty, '.aitelier-secrets'));
  writeFileSync(join(empty, '.aitelier-secrets', 'wechat-bridge-token'), 'throwaway\n');
  assert.ok(run({ WECHAT_BRIDGE_SAVE_LOGIN: '0', HOME: empty }).includes('aitelier-wechat-bridge:local'));
  assert.ok(!existsSync(join(empty, '.local')), 'off creates nothing');
  assert.throws(() => execFileSync('sh', [script], { encoding: 'utf8', stdio: 'pipe',
    env: { PATH: `${join(home, 'bin')}:/usr/bin:/bin`, HOME: home, WECHAT_BRIDGE_SAVE_LOGIN: 'off' } }), error => error.status === 78);
});
