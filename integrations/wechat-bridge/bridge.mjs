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
 *  the real puppet. `renderQr` (qr-svg.mjs, also handed in) draws the login QR as an
 *  SVG for the dashboard. */
import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';

// Loopback by default: the token is the only lock, so the socket must not be
// reachable from another machine. Inside its container the bridge has to listen on
// the container's interface (its loopback cannot be published); there, and only
// there, WECHAT_BRIDGE_BIND=0.0.0.0 is accepted, and the run script publishes the
// port on the host's 127.0.0.1 only.
export const HOST = '127.0.0.1';
export const CONTAINER_HOST = '0.0.0.0';
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

/** The address to listen on: `value` (WECHAT_BRIDGE_BIND) unset or 127.0.0.1 is
 *  loopback; 0.0.0.0 only when `inContainer`; anything else is refused. */
export function bindHost(value, inContainer) {
  if (!value || value === HOST) return HOST;
  if (value === CONTAINER_HOST && inContainer) return CONTAINER_HOST;
  throw new Error(`wechat-bridge refused: bind address ${JSON.stringify(value)} (only ${HOST}, or ${CONTAINER_HOST} inside the container)`);
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
  log = () => {}, renderQr = () => undefined }) {
  if (typeof token !== 'string' || !token.trim()) throw new Error('wechat-bridge refused: no token');
  const expected = digest(token.trim());
  const ring = [];
  // Sequence numbers start at the boot time in ms: a cursor a client kept from
  // before a bridge restart is below every new number, so a restart never hides
  // new messages behind an old cursor (and needs no reset rule).
  let seq = now().getTime();
  let waiters = new Set();
  const state = { logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined, since: now().toISOString() };
  const setState = change => Object.assign(state, change, { since: now().toISOString() });

  // The QR as an image, or undefined when it cannot be drawn (never logged: it is a login token).
  const drawn = qrcode => {
    try { return renderQr(qrcode) || undefined; }
    catch { log('the login QR could not be drawn'); return undefined; }
  };
  bot.on('scan', (qrcode, status) => {
    // Only a waiting QR can be scanned; once scanned, timed out or cancelled it is gone.
    const qr = status === types.ScanStatus.Waiting && qrcode ? qrcode : undefined;
    setState({ logged_in: false, user_id: undefined, qr, qr_svg: qr ? drawn(qr) : undefined });
  });
  bot.on('login', user => setState({ logged_in: true, user_id: user?.id, qr: undefined, qr_svg: undefined }));
  bot.on('logout', () => setState({ logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined }));
  bot.on('message', message => {
    try {
      if (!isOwnerCandidate(message, types)) return;
      // When the owner wrote it, as WeChat says; a message WeChat does not date carries no
      // time (never the bridge's own: the host refuses an undated approval phrase).
      const date = message.date?.();
      const at = date instanceof Date && !Number.isNaN(date.getTime()) ? date.toISOString() : undefined;
      ring.push({ seq: ++seq, type: 'message', sender_ref: message.talker().id, message_ref: String(message.id),
        text: message.text(), ...(at ? { at } : {}) });
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
      let sent;
      try { sent = await contact.say(part); }
      catch (error) {
        // Wechaty can still hold a contact that neo has dropped from its own list of
        // observed peers; neo then refuses the peer, which is not a failed send.
        if (ref === null && /observed peer/.test(String(error?.message))) return [409, { error: 'recipient_unknown' }];
        throw error;
      }
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
      if (size > MAX_BODY_BYTES) return;
      size += chunk.length;
      // The rest of an oversized body is read and dropped, so the 413 reaches the client.
      if (size > MAX_BODY_BYTES) { chunks.length = 0; reject(Object.assign(new Error('body_too_large'), { status: 413 })); return; }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (size > MAX_BODY_BYTES) return;
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
        if (state.qr_svg) body.qr_svg = state.qr_svg;
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
    /** Long polls waiting now (each holds a timer until it is answered or closed). */
    get waiting() { return waiters.size; },
    listen: (port = DEFAULT_PORT, host = HOST) => new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.off('error', reject); resolve(server.address()); });
    }),
    close: () => new Promise(resolve => {
      for (const wake of waiters) wake();
      server.closeAllConnections?.();
      server.close(() => resolve());
    }),
  };
}

/** Ends the process (through `stop(reason, 75)`) whenever this login cannot go on:
 *  puppet-neo needs a fresh start for a new QR. That is a logout, a QR nobody scanned
 *  in time (or cancelled), and a puppet error before any login: neo then gives up its
 *  session without a logout event, and without this the bridge would stay up with no
 *  login and no QR. Errors while logged in are only logged (Wechaty reports message
 *  handling failures that way). Whether and how often to start again is the
 *  container's restart policy. */
export function superviseLogin({ bot, types, stop, log = () => {} }) {
  let loggedIn = false;
  bot.on('scan', (_qrcode, status) => {
    if (status === types.ScanStatus.Waiting) log('login QR waiting (GET /status carries it)');
    if (status === types.ScanStatus.Timeout || status === types.ScanStatus.Cancel) void stop('login QR ended without a login', 75);
  });
  bot.on('login', () => { loggedIn = true; log('logged in'); });
  bot.on('logout', () => { loggedIn = false; void stop('logged out', 75); });
  bot.on('error', error => {
    log('error: ' + String(error?.message ?? error).slice(0, 200));
    if (!loggedIn) void stop('the puppet failed before a login', 75);
  });
}
