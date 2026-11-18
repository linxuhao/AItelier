/** wechat-bridge: one WeChat login kept alive in its own process, served over
 *  loopback HTTP to the steward's channel plugin (dsh-channel-wechat).
 *
 *  Why a separate process: puppet-neo keeps the login in memory only, so whoever
 *  holds it must outlive steward and host restarts, or every restart would need a
 *  new QR scan (design §7.2).
 *
 *  This file is the HTTP layer and the event filter, with no dependency of its own:
 *  `bot` is a Wechaty instance (or a fake with the same events and Contact.find) and
 *  `types` is Wechaty's `types`, both handed in by main.mjs, so the tests never load
 *  the real puppet. */
import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';

// Loopback only, and not configurable: the token is the only lock, so the socket
// must not be reachable from another machine even by a mistaken setting.
export const HOST = '127.0.0.1';
export const DEFAULT_PORT = 18731;
export const RING_SIZE = 500;
export const MAX_WAIT_SECONDS = 25;
// WeChat cuts long texts in the client; splitting here keeps each part whole.
export const SEND_CHUNK = 1500;
const MAX_BODY_BYTES = 64 * 1024;

/** Texts of more than `size` characters, cut into consecutive parts of at most
 *  `size` code points (a surrogate pair is never split). */
export function splitText(text, size = SEND_CHUNK) {
  const points = Array.from(text);
  const parts = [];
  for (let i = 0; i < points.length; i += size) parts.push(points.slice(i, i + size).join(''));
  return parts.length ? parts : [''];
}

const digest = value => createHash('sha256').update(String(value)).digest();

/** Whether this WeChat message is one the owner could have written to the bot:
 *  a 1:1 text from someone else. Rooms, the account's own messages (also those it
 *  sends from the phone) and official accounts are never relayed. Neo reports every
 *  peer as contact type Unknown, so official accounts are recognised by WeChat's
 *  `gh_` id prefix as well as by type. */
export function isOwnerCandidate(message, types) {
  if (message.self() || message.room()) return false;
  if (message.type() !== types.Message.Text) return false;
  const talker = message.talker();
  if (!talker?.id || talker.id.startsWith('gh_')) return false;
  return talker.type() !== types.Contact.Official;
}

export function createBridge({ bot, types, token, ringSize = RING_SIZE, maxWaitSeconds = MAX_WAIT_SECONDS, now = () => new Date(),
  log = () => {} }) {
  if (typeof token !== 'string' || !token.trim()) throw new Error('wechat-bridge refused: no token');
  const expected = digest(token.trim());
  const ring = [];
  // Sequence numbers start at the boot time in ms: a cursor a client kept from
  // before a bridge restart is below every new number, so a restart never hides
  // new messages behind an old cursor (and needs no reset rule).
  let seq = now().getTime();
  let waiters = new Set();
  const state = { logged_in: false, user_id: undefined, qr: undefined, since: now().toISOString() };
  const setState = change => Object.assign(state, change, { since: now().toISOString() });

  bot.on('scan', (qrcode, status) => {
    // Only a waiting QR can be scanned; once scanned, timed out or cancelled it is gone.
    setState({ logged_in: false, user_id: undefined, qr: status === types.ScanStatus.Waiting && qrcode ? qrcode : undefined });
  });
  bot.on('login', user => setState({ logged_in: true, user_id: user?.id, qr: undefined }));
  bot.on('logout', () => setState({ logged_in: false, user_id: undefined, qr: undefined }));
  bot.on('message', message => {
    try {
      if (!isOwnerCandidate(message, types)) return;
      const date = message.date?.();
      const at = date instanceof Date && !Number.isNaN(date.getTime()) ? date.toISOString() : now().toISOString();
      ring.push({ seq: ++seq, type: 'message', sender_ref: message.talker().id, message_ref: String(message.id),
        text: message.text(), at });
      if (ring.length > ringSize) ring.splice(0, ring.length - ringSize);
      const pending = waiters;
      waiters = new Set();
      for (const wake of pending) wake();
    } catch (error) {
      // Never the text: only that a message could not be read.
      log('message skipped: ' + String(error?.message ?? error).slice(0, 200));
    }
  });

  const after = cursor => ring.filter(event => event.seq > cursor);

  // `response` closes before we answer only when the client went away; a request's
  // own 'close' can fire as soon as its (empty) body is read, so it is not used.
  async function events(cursor, waitSeconds, response) {
    let found = after(cursor);
    if (!found.length && waitSeconds > 0) {
      await new Promise(resolve => {
        const done = () => { clearTimeout(timer); waiters.delete(done); response.off('close', done); resolve(); };
        const timer = setTimeout(done, waitSeconds * 1000);
        waiters.add(done);
        response.on('close', done);
      });
      found = after(cursor);
    }
    return { events: found, next_after: found.length ? found.at(-1).seq : cursor };
  }

  async function send(to, text) {
    if (!state.logged_in) return [409, { error: 'not_logged_in' }];
    // Neo can only write to a peer it has seen in this login (a message from them).
    const contact = await bot.Contact.find({ id: to });
    if (!contact) return [409, { error: 'recipient_unknown' }];
    let ref = null;
    for (const part of splitText(text)) {
      const sent = await contact.say(part);
      ref ??= sent?.id ?? null;
    }
    return [200, { ref }];
  }

  const reply = (response, status, body) => {
    response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    response.end(JSON.stringify(body));
  };

  const readJson = request => new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) { reject(Object.assign(new Error('body_too_large'), { status: 413 })); request.destroy(); return; }
      chunks.push(chunk);
    });
    request.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(Object.assign(new Error('bad_request'), { status: 400 })); }
    });
    request.on('error', reject);
  });

  const authorized = request => {
    const header = request.headers.authorization ?? '';
    const match = /^Bearer (.+)$/.exec(header);
    return Boolean(match) && timingSafeEqual(digest(match[1].trim()), expected);
  };

  const server = createServer(async (request, response) => {
    try {
      if (!authorized(request)) return reply(response, 401, { error: 'unauthorized' });
      const url = new URL(request.url, 'http://bridge');
      if (request.method === 'GET' && url.pathname === '/status') {
        const body = { logged_in: state.logged_in, since: state.since };
        if (state.user_id) body.user_id = state.user_id;
        if (state.qr) body.qr = state.qr;
        return reply(response, 200, body);
      }
      if (request.method === 'GET' && url.pathname === '/events') {
        const cursor = Number(url.searchParams.get('after') ?? 0);
        const wait = Number(url.searchParams.get('wait') ?? 0);
        if (!Number.isFinite(cursor) || !Number.isFinite(wait) || wait < 0) return reply(response, 400, { error: 'bad_request' });
        return reply(response, 200, await events(cursor, Math.min(wait, maxWaitSeconds), response));
      }
      if (request.method === 'POST' && url.pathname === '/send') {
        const body = await readJson(request);
        if (typeof body?.to !== 'string' || !body.to || typeof body.text !== 'string' || !body.text.trim()) {
          return reply(response, 400, { error: 'bad_request' });
        }
        const [status, result] = await send(body.to, body.text);
        return reply(response, status, result);
      }
      return reply(response, 404, { error: 'not_found' });
    } catch (error) {
      if (error?.status) return reply(response, error.status, { error: error.message });
      // A failed send reaches the caller as one error; the text is never logged.
      log('request failed: ' + String(error?.message ?? error).slice(0, 200));
      return reply(response, 502, { error: 'send_failed', detail: String(error?.message ?? error).slice(0, 200) });
    }
  });

  return {
    server,
    state,
    listen: (port = DEFAULT_PORT) => new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, HOST, () => { server.off('error', reject); resolve(server.address()); });
    }),
    close: () => new Promise(resolve => {
      for (const wake of waiters) wake();
      server.closeAllConnections?.();
      server.close(() => resolve());
    }),
  };
}
