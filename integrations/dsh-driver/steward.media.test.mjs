/** Step 24.2 (design-steward 7.8.12, 7.8.17 "适配器"): the AItelier steward host's media side -
 *  the `media` capability exists only with a media root (AITELIER_MEDIA_ROOT, given by the DSH
 *  host), the gateway fetch stores what /fetch answered as `generated` under the turn's
 *  audience, the group's send_media record, the relays carry attachment metadata only when
 *  there is some, storeMedia's binding read, and a wake shows attachments one line each.
 *  Step 24.2b: an owner turn's send_media as one steward message with the file, mirrored. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { aitelierHost, DriverRuntime } from './runtime.mjs';
import { channelOps, forwardFinalText, groupOps, mediaOps, mediaRootFrom, stateWake, unansweredOwnerMessages, withAttachmentLines } from './steward-host.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { validateHost } from '../dsh-director/lib/host.mjs';
import { createRequire } from 'node:module';
import { measuredEntryForTest } from './dsh-entry.mjs';
import { registerMediaTools } from '../dsh-director/lib/media-tools.mjs';
import { createOwnerChannels } from '../dsh-director/lib/channels.mjs';
import { statusError } from '../dsh-director/lib/core.mjs';

const env = (extra = {}) => ({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
  AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home',
  AITELIER_DSH_EXECUTION: 'host', ...extra });
const PNG = Buffer.concat([Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex'), Buffer.from([0, 0, 0, 64, 0, 0, 0, 64]), Buffer.alloc(40)]);

test('the media capability: a steward with a media root has it (and validates); without a root it has none', () => {
  const root = mkdtempSync(join(tmpdir(), 'steward-media-'));
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: root }));
  assert.deepEqual(steward.capabilities.slice(-8), ['memory', 'portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog', 'media']);
  for (const op of ['ingest', 'resolve', 'read', 'fetch', 'postGroup', 'setProtected']) assert.equal(typeof steward.media[op], 'function', op);
  assert.equal(typeof steward.channels.senderBound, 'function');
  assert.doesNotThrow(() => validateHost(steward));
  const bare = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }));
  assert.ok(!bare.capabilities.includes('media')); assert.equal(bare.media, undefined);
});

test('24.2b: a director with a media root has its own d-<driver_id> library (1 GiB), no gateway and no group', async () => {
  // design-steward 7.8.12a "一个写者一个子目录": one writer per subdirectory.
  const root = mkdtempSync(join(tmpdir(), 'director-media-'));
  const director = aitelierHost(env({ AITELIER_MEDIA_ROOT: root }));
  assert.equal(director.capabilities.at(-1), 'media');
  assert.doesNotThrow(() => validateHost(director));
  for (const op of ['ingest', 'resolve', 'read', 'fetch', 'setProtected']) assert.equal(typeof director.media[op], 'function', op);
  assert.equal(director.media.postGroup, undefined, 'a director has no group to post to');
  const { ref } = await director.media.ingest({ channel: 'director', messageRef: 'k1', index: 0, audience: 'project:aitelier-home', source: 'generated' }, async () => PNG);
  assert.match(ref, /^m_[0-9a-f]{16}$/);
  assert.ok(existsSync(join(root, 'd-drv-home', 'index.jsonl')), 'the director writes d-<driver_id>/');
  assert.equal(existsSync(join(root, 'owner')), false, 'never the steward\'s subdirectory');
  assert.equal((await director.media.resolve(ref)).audience, 'project:aitelier-home');
  const fetched = await director.media.fetch({ connId: 'x', url: 'https://example.invalid/a.png', audience: 'project:aitelier-home' });
  assert.equal(fetched.refused, 'fetch_failed', 'no gateway');
  // The library under the adapter is held to the director's 1 GiB.
  assert.equal(createMediaLibrary({ root, userId: 'd-drv-home' }).limits.total_bytes, 1024 * 1024 * 1024);
  // No media root, or a driver id that cannot name a subdirectory: no library, no capability.
  for (const extra of [{}, { AITELIER_MEDIA_ROOT: root, AITELIER_DRIVER_ID: '../x' }, { AITELIER_MEDIA_ROOT: root, AITELIER_DRIVER_ID: '' }]) {
    const bare = aitelierHost(env(extra));
    assert.ok(!bare.capabilities.includes('media'), JSON.stringify(extra)); assert.equal(bare.media, undefined);
  }
});

test('ingest through the host adapter keeps the caller\'s signal: already aborted is unavailable, open never called, nothing stored', async () => {
  // Review T2: host.d.ts media.ingest(meta, open, {signal}) - the adapter the plugin is given
  // (aitelierHost(...).media, not a fixture of its own) must hand the signal to the library.
  const root = mkdtempSync(join(tmpdir(), 'steward-media-'));
  const host = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: root }));
  let opened = 0;
  const open = async () => { opened += 1; return PNG; };
  const meta = { channel: 'wechat', messageRef: 'w1', index: 0, audience: 'owner', source: 'in' };
  assert.deepEqual(await host.media.ingest(meta, open, { signal: AbortSignal.abort() }), { refused: 'unavailable' });
  assert.equal(opened, 0, 'not one byte was asked for');
  assert.equal(existsSync(join(root, 'owner', 'index.jsonl')), false, 'no row');
  // Without a signal (or a live one) the same call stores it.
  const stored = await host.media.ingest(meta, open, { signal: new AbortController().signal });
  assert.match(stored.ref, /^m_[0-9a-f]{16}$/); assert.equal(opened, 1);
});

test('events(): the media library\'s cleanup runs at the start (an expired row and its file go) and hourly after; stop() ends it', async () => {
  // Review T3: runtime.mjs library?.start() / library?.stop(), through the adapter's own events()
  // and the stop it returns - not the library's cleanup() called by hand.
  // design/steward-group-media.md section 4: hourly, so a kept medium is on disk at most 25 hours.
  const HOUR = 3600 * 1000, DAY = 24 * HOUR;
  const root = mkdtempSync(join(tmpdir(), 'steward-media-')), stateDir = mkdtempSync(join(tmpdir(), 'steward-state-'));
  const old = createMediaLibrary({ root, now: () => Date.now() - 40 * DAY });
  const { ref } = await old.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const file = old.lookup(ref).path;
  // A fake State: the reads events() makes answer empty; the subscription parks until stopped.
  const request = async (path, body, signal) => {
    if (path === '/api/drivers/me') return { driver_id: 'drv-home' };
    if (path.endsWith('/context')) return { project_id: 'aitelier-home', driver_id: 'drv-home', event_seq: 1, nodes: [],
      project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'protocol' };
    if (path.endsWith('/activity') || path.endsWith('/heartbeat')) return { ok: true };
    if (path === '/api/state/query/list_director_messages') return { result: { items: [], matched_total: 0, has_more: false } };
    if (path === '/api/state/query/wait_for_state_change') return new Promise((_, reject) =>
      signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true }));
    return { result: {} };
  };
  const runtime = new DriverRuntime({ project: 'aitelier-home', driver: 'drv-home', directory: stateDir, request, retryDelayMs: 10 });
  await runtime.init();
  const host = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: root, AITELIER_DSH_STATE_DIR: stateDir }), { request, runtime });
  assert.equal((await host.media.resolve(ref))?.ref, ref, 'the expired row is there before events()');
  const api = { notify: async () => {}, eventMessage: text => text, setHostError: async () => {}, clearHostError: async () => {},
    isDisposed: () => false, ownerMessageSent: async () => {}, agent: { session: { snapshotEvents: () => [] }, steer: () => {} },
    ctx: { logger: { warn() {}, info() {}, error() {} }, on: () => () => {}, goals: { get: () => undefined },
      agents: { withoutInitiator: fn => fn() }, sessionPersistence: { flush: async () => {} } } };
  const armed = [], cleared = [], { setInterval: realSet, clearInterval: realClear } = globalThis;
  globalThis.setInterval = (fn, ms, ...rest) => { const timer = realSet(fn, ms, ...rest); if (ms === HOUR) armed.push(timer); return timer; };
  globalThis.clearInterval = timer => { if (armed.includes(timer)) cleared.push(timer); return realClear(timer); };
  try {
    const events = await host.events(api);
    assert.equal(await host.media.resolve(ref), undefined, 'the start\'s cleanup dropped the 40-day-old row');
    assert.equal(existsSync(file), false, 'and its file');
    assert.equal(armed.length, 1, 'the hourly cleanup is armed');
    assert.equal(cleared.length, 0);
    events.stop();
    await Promise.resolve(events.done).catch(() => {});
    assert.deepEqual(cleared, armed, 'stop() ends the hourly cleanup');
  } finally { Object.assign(globalThis, { setInterval: realSet, clearInterval: realClear }); }
});

test('24.2c standing route check, wired: the steward\'s route reads raise the dashboard flag in its own slot (held until events()), a restored connection clears only the host error, images again clear the flag; a director is not checked', async () => {
  const realNow = Date.now;
  let clock = realNow(), routes = { root: ['text', 'image'], child: ['text'] }, reads = 0;
  Date.now = () => clock;
  try {
    const stateDir = mkdtempSync(join(tmpdir(), 'steward-state-'));
    const request = async (path, body, signal) => {
      if (path === '/api/dsh-drivers/aitelier-home/route-input' || path === '/api/dsh-drivers/alpha/route-input') { reads += 1; return routes; }
      if (path === '/api/drivers/me') return { driver_id: 'drv-home' };
      if (path.endsWith('/context')) return { project_id: 'aitelier-home', driver_id: 'drv-home', event_seq: 1, nodes: [],
        project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'protocol' };
      if (path.endsWith('/activity') || path.endsWith('/heartbeat')) return { ok: true };
      if (path === '/api/state/query/list_director_messages') return { result: { items: [], matched_total: 0, has_more: false } };
      if (path === '/api/state/query/wait_for_state_change') return new Promise((_, reject) =>
        signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true }));
      return { result: {} };
    };
    const boot = async role => {
      const runtime = new DriverRuntime({ project: 'aitelier-home', driver: 'drv-home', directory: stateDir, request, retryDelayMs: 10 });
      await runtime.init();
      const host = aitelierHost(env({ ...(role ? { AITELIER_DSH_ROLE: role } : {}), AITELIER_MEDIA_ROOT: mkdtempSync(join(tmpdir(), 'steward-media-')),
        AITELIER_DSH_STATE_DIR: stateDir }), { request, runtime });
      // The plugin's three status slots (index.mjs): completion/host error, then the route flag.
      const calls = [], slots = { host: '', route: '' };
      const api = { notify: async () => {}, eventMessage: text => text, isDisposed: () => false, ownerMessageSent: async () => {},
        setHostError: async message => { calls.push(['set', message]); slots.host = message; },
        clearHostError: async () => { calls.push(['clear']); slots.host = ''; },
        setRouteFlag: async text => { calls.push(['route', text]); slots.route = text ?? ''; },
        agent: { session: { snapshotEvents: () => [] }, steer: () => {} },
        ctx: { logger: { warn() {}, info() {}, error() {} }, on: () => () => {}, goals: { get: () => undefined },
          agents: { withoutInitiator: fn => fn() }, sessionPersistence: { flush: async () => {} } } };
      return { host, runtime, calls, api, shown: () => statusError(slots) };
    };
    const steward = await boot('steward');
    // A read before events() (the first step can come first): the flag waits for the status seam.
    assert.equal(await steward.host.media.takesImages(), false);
    assert.equal(reads, 1);
    const events = await steward.host.events(steward.api);
    try {
      const flag = ['route', '管家路线不收图（child text-only）'];
      assert.deepEqual(steward.calls, [flag], 'events() puts the held flag on the dashboard status');
      await steward.host.media.takesImages();
      assert.deepEqual(steward.calls, [flag], 'a read with the same answer writes nothing');
      await steward.runtime.onConnectionRestored();
      assert.deepEqual(steward.calls.at(-1), ['clear'], 'a restored connection clears the host error only');
      assert.equal(steward.shown(), flag[1], 'the flag stays on the status');
      clock += 60000; routes = { root: ['text', 'image'], child: ['text', 'image'] };
      assert.equal(await steward.host.media.takesImages(), true);
      await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(steward.calls.at(-1), ['route', undefined], 'images again: the flag is cleared');
      const before = steward.calls.length;
      await steward.runtime.onConnectionRestored();
      assert.deepEqual(steward.calls.slice(before), [['clear']], 'no flag: a restored connection clears as before');
      clock += 60000; routes = { root: ['text', 'image'], child: ['text', 'image'], measured: false };
      await steward.host.media.takesImages();
      await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(steward.calls.at(-1), ['route', '管家路线不收图（measured:false）']);
    } finally {
      events.stop();
      await Promise.resolve(events.done).catch(() => {});
    }
    // A director: its route reads are not checked (it has no takesImages), and a restored
    // connection clears the host error exactly as before.
    const director = await boot(undefined);
    assert.equal(director.host.media.takesImages, undefined);
    // The owner cleared the director's goal (bindDriverGoal then binds none): no goal service needed.
    director.api.agent.session.snapshotEvents = () => [{ type: 'goal/change', data: { operation: 'clear' } }];
    const directorEvents = await director.host.events(director.api);
    try {
      await director.runtime.onConnectionRestored();
      assert.deepEqual(director.calls, [['clear']]);
    } finally {
      directorEvents.stop();
      await Promise.resolve(directorEvents.done).catch(() => {});
    }
  } finally { Date.now = realNow; }
});

test('MP-1: the route flag and the host error never erase each other - a group-setup recovery, a connection outage, and the route changing during one', async () => {
  const realNow = Date.now;
  let clock = realNow(), routes = { root: ['text'], child: ['text'] };
  Date.now = () => clock;
  try {
    const stateDir = mkdtempSync(join(tmpdir(), 'steward-state-'));
    const request = async (path, body, signal) => {
      if (path === '/api/dsh-drivers/aitelier-home/route-input') return routes;
      if (path === '/api/drivers/me') return { driver_id: 'drv-home' };
      if (path.endsWith('/context')) return { project_id: 'aitelier-home', driver_id: 'drv-home', event_seq: 1, nodes: [],
        project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'protocol' };
      if (path.endsWith('/activity') || path.endsWith('/heartbeat')) return { ok: true };
      if (path === '/api/state/query/list_director_messages') return { result: { items: [], matched_total: 0, has_more: false } };
      if (path === '/api/state/query/wait_for_state_change') return new Promise((_, reject) =>
        signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true }));
      return { result: {} };
    };
    const runtime = new DriverRuntime({ project: 'aitelier-home', driver: 'drv-home', directory: stateDir, request, retryDelayMs: 10 });
    await runtime.init();
    const host = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: mkdtempSync(join(tmpdir(), 'steward-media-')),
      AITELIER_DSH_STATE_DIR: stateDir }), { request, runtime });
    // index.mjs's slots and the status line they make.
    const slots = { host: '', route: '' };
    const api = { notify: async () => {}, eventMessage: text => text, isDisposed: () => false, ownerMessageSent: async () => {},
      setHostError: async message => { slots.host = message; }, clearHostError: async () => { slots.host = ''; },
      setRouteFlag: async text => { slots.route = text ?? ''; },
      agent: { session: { snapshotEvents: () => [] }, steer: () => {} },
      ctx: { logger: { warn() {}, info() {}, error() {} }, on: () => () => {}, goals: { get: () => undefined },
        agents: { withoutInitiator: fn => fn() }, sessionPersistence: { flush: async () => {} } } };
    const shown = () => statusError(slots);
    const settle = () => new Promise(resolve => setImmediate(resolve));
    const FLAG = '管家路线不收图（root text-only, child text-only）';
    const events = await host.events(api);
    try {
      await host.media.takesImages(); await settle();
      assert.equal(shown(), FLAG);
      // (a) a group-turn setup failure, then its recovery (lib/group-turns.mjs: set, then clear).
      await api.setHostError('group_turn_setup_failed: boom');
      assert.equal(shown(), 'group_turn_setup_failed: boom\n' + FLAG, 'both, each on its own line');
      await api.clearHostError();
      for (let i = 0; i < 5; i += 1) { clock += 60000; await host.media.takesImages(); }
      await settle();
      assert.equal(shown(), FLAG, 'the recovery leaves the flag while the route is still text-only');
      // (b) the connection is lost, and the route is fixed during the outage.
      await runtime.onConnectionError(new Error('socket hang up'));
      const lost = slots.host;
      assert.ok(lost, 'the connection error is shown');
      clock += 60000; routes = { root: ['text', 'image'], child: ['text', 'image'] };
      assert.equal(await host.media.takesImages(), true); await settle();
      assert.equal(shown(), lost, 'the route recovering does not erase the connection error');
      // (c) the route breaks again during the same outage: both are shown.
      clock += 60000; routes = { root: ['text'], child: ['text'] };
      await host.media.takesImages(); await settle();
      assert.equal(shown(), lost + '\n' + FLAG, 'the flag does not replace the connection error');
      await runtime.onConnectionRestored();
      assert.equal(shown(), FLAG, 'the connection back: only the flag is left');
    } finally {
      events.stop();
      await Promise.resolve(events.done).catch(() => {});
    }
  } finally { Date.now = realNow; }
});

test('statusError: the completion or host error, then the route flag on its own line, within the backend\'s 2000 characters (the flag is never cut)', () => {
  assert.equal(statusError({}), '');
  assert.equal(statusError({ host: 'h' }), 'h');
  assert.equal(statusError({ completion: 'c', host: 'h' }), 'c');
  assert.equal(statusError({ route: 'r' }), 'r');
  assert.equal(statusError({ completion: 'c', host: 'h', route: 'r' }), 'c\nr');
  const long = statusError({ host: 'x'.repeat(5000), route: '管家路线不收图（root text-only）' });
  assert.equal(long.length, 2000);
  assert.ok(long.endsWith('\n管家路线不收图（root text-only）'));
  assert.equal(statusError({ host: 'y'.repeat(5000) }).length, 2000);
});

test('the media root comes from AITELIER_MEDIA_ROOT (absolute) or there is none', () => {
  assert.equal(mediaRootFrom({ AITELIER_MEDIA_ROOT: '/srv/media' }), '/srv/media');
  assert.equal(mediaRootFrom({ AITELIER_MEDIA_ROOT: 'relative' }), undefined);
  assert.equal(mediaRootFrom({}), undefined);
});

test('fetch: what the gateway answered is stored as generated under the turn\'s audience; a refusal is passed on', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const asked = [];
  let answer = () => new Response(PNG, { status: 200, headers: { 'content-type': 'image/png' } });
  const ops = mediaOps({ request: async () => ({}), library, gatewayFetch: async (connId, url) => { asked.push([connId, url]); return answer(); } });
  const stored = await ops.fetch({ connId: 'media', url: 'http://mcp_server:9003/files/a.png', audience: 'group:wechat:r1' });
  assert.match(stored.ref, /^m_[0-9a-f]{16}$/);
  assert.deepEqual(asked, [['media', 'http://mcp_server:9003/files/a.png']]);
  const row = await ops.resolve(stored.ref);
  assert.equal(row.source, 'generated'); assert.equal(row.audience, 'group:wechat:r1'); assert.equal(row.channel, 'generated');
  assert.deepEqual(readFileSync(row.path), PNG);
  answer = () => new Response(JSON.stringify({ detail: 'url_not_seen' }), { status: 403 });
  assert.deepEqual(await ops.fetch({ connId: 'media', url: 'http://x/y', audience: 'owner' }), { refused: 'url_not_seen' });
  answer = () => { throw new Error('ECONNREFUSED'); };
  assert.equal((await ops.fetch({ connId: 'media', url: 'http://x/y', audience: 'owner' })).refused, 'fetch_failed');
});

test('postGroup: post_group_media with the turn, the kind and the caption; a State refusal is {refused}', async () => {
  const sent = [];
  const ops = mediaOps({ library: createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) }), gatewayFetch: async () => {},
    request: async (path, body) => { sent.push([path, body]); return { result: { posted: true, channel: 'wechat', group_ref: 'r1' } }; } });
  assert.deepEqual(await ops.postGroup({ turn: 't7', kind: 'image', caption: '猫' }), { posted: true, channel: 'wechat', group_ref: 'r1' });
  assert.equal(sent[0][0], '/api/state/commands/post_group_media');
  assert.equal(sent[0][1].turn, 't7'); assert.equal(sent[0][1].kind, 'image'); assert.equal(sent[0][1].caption, '猫');
  assert.match(sent[0][1].request_key, /^group-media:t7:/);
  const refusing = mediaOps({ library: createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) }), gatewayFetch: async () => {},
    request: async () => { throw new Error('AItelier /x: HTTP 422 "group_send_budget"'); } });
  assert.equal((await refusing.postGroup({ turn: 't7', kind: 'image' })).refused, 'group_send_budget');
  // checkOnly (review F2): the dry run before a fetch - check_only, its own key, kind optional.
  await ops.postGroup({ turn: 't7', caption: '猫', checkOnly: true });
  assert.equal(sent[1][1].check_only, true); assert.ok(!('kind' in sent[1][1]));
  assert.match(sent[1][1].request_key, /^group-media-check:t7:/);
  assert.ok(!('check_only' in sent[0][1]), 'the real record carries no check_only');
});

test('the relays carry attachments only when there are some; storeMedia\'s binding read', async () => {
  const sent = [];
  const request = async (path, body) => { sent.push([path, body]); return { result: { message_id: 'm1', bound: true } }; };
  const channels = channelOps(request), groups = groupOps({ request, project: 'aitelier-home', me: 'drv' });
  await channels.relay({ channel: 'wechat', senderRef: 'w', messageRef: 'r', text: '你好' });
  assert.ok(!('attachments' in sent.at(-1)[1]), 'a message without files is the request it always was');
  const files = [{ kind: 'image', ref: 'm_0123456789abcdef', mime: 'image/png', size: 9 }];
  await channels.relay({ channel: 'wechat', senderRef: 'w', messageRef: 'r2', text: '[图片]', attachments: files });
  assert.deepEqual(sent.at(-1)[1].attachments, files);
  await groups.relay({ channel: 'wechat', groupRef: 'g', senderRef: 'w', messageRef: 'r3', text: '[图片]', attachments: files });
  assert.deepEqual(sent.at(-1)[1].attachments, files);
  assert.deepEqual(await channels.senderBound({ channel: 'wechat', senderRef: 'w' }), { message_id: 'm1', bound: true });
  assert.deepEqual(sent.at(-1), ['/api/state/query/owner_channel_sender_bound', { channel: 'wechat', sender_ref: 'w' }]);
});

test('a wake shows each owner message\'s attachments one line each, from the source rows', async () => {
  const ref = 'm_3f9a000000000000';
  const request = async (path, body) => {
    assert.equal(path, '/api/state/query/message_sources');
    assert.deepEqual(body.message_ids, ['u1', 'u2']);
    return { result: { sources: { u1: { channel: 'wechat', attachments: [{ kind: 'voice', ref, durationMs: 23000 },
      { kind: 'image', refused: 'too_large' }] }, u2: { channel: 'wechat' } } } };
  };
  const result = { events: [], driver_inbox: [{ message_id: 'u1', sender_kind: 'user', body: '[语音 0:23]\n[图片 过大未收]' },
    { message_id: 'u2', sender_kind: 'user', body: 'hi' }, { message_id: 'd1', sender_kind: 'driver', body: 'x' }] };
  const shown = await withAttachmentLines(request, result);
  assert.deepEqual(shown.driver_inbox[0].attachments, [`[语音 ${ref}，0:23]`, '[图片 过大未收]']);
  assert.ok(!('attachments' in shown.driver_inbox[1]) && !('attachments' in shown.driver_inbox[2]));
  assert.equal(await withAttachmentLines(async () => { throw new Error('down'); }, result), result, 'a failed read leaves the wake as it was');
  const none = { events: [{ event_type: 'x' }] };
  assert.equal(await withAttachmentLines(async () => { throw new Error('not called'); }, none), none);
});

test('a wake\'s id is the digest of the batch as State answered it: with or without the attachment lines, a replay is the same wake', async () => {
  const result = { events: [{ event_type: 'user_message_received' }], driver_inbox: [{ message_id: 'u1', sender_kind: 'user', body: '[语音 0:23]' }] };
  const inbox = { items: [] };
  const request = async () => ({ result: { sources: { u1: { attachments: [{ kind: 'voice', ref: 'm_3f9a000000000000', durationMs: 23000 }] } } } });
  const read = stateWake(result, inbox, await withAttachmentLines(request, result));
  const failed = stateWake(result, inbox, await withAttachmentLines(async () => { throw new Error('down'); }, result));
  assert.ok(read.text.includes('m_3f9a000000000000') && !failed.text.includes('m_3f9a000000000000'));
  assert.equal(read.id, failed.id, 'the read succeeding once and failing once is still one wake');
  assert.match(read.id, /^state:[0-9a-f]{40}$/);
  // Without a media library the wake is the one it always was (text and id).
  const plain = stateWake(result, inbox);
  assert.equal(plain.text, 'AItelier runtime event:\n' + JSON.stringify({ ...result, project_inbox: inbox }));
  assert.equal(plain.id, read.id);
  // A batch that grew is a new wake.
  assert.notEqual(stateWake({ ...result, events: [...result.events, { event_type: 'x' }] }, inbox).id, plain.id);
});

// 2026-11-22: media_call's `media` once declared additionalProperties as a schema; the pinned
// DSH accepts only true/false there, so defineTool threw at the steward's boot and the steward
// never started. The other media tests pass definitions through `t => t`; this one uses DSH's own.
test('the media tools are accepted by the pinned DSH defineTool', async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const { defineTool } = await import(createRequire(entry).resolve('@deepseek-ai/dsh-tools'));
  const names = [];
  const jsonOutput = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
  registerMediaTools({ tools: { register: tool => names.push(tool.name) } }, { ctx: {}, defineTool, jsonOutput, HarnessError: Error,
    media: {}, catalog: {}, turns: {}, channels: undefined, ledger: {} });
  assert.deepEqual(names, ['media_call', 'send_media', 'read_document', 'make_document']);
});

// --- step 24.2b (design-steward 7.8.12a "出站", 7.8.18 适配器与插件): an owner turn's send_media is
// ONE steward message with the file (send_user_message attachments), mirrored by the plugin;
// a group turn's is unchanged (post_group_media, the group's sendFile). Real pieces: the
// adapter's mediaOps over a real library, the plugin's media tools and ownerChannels; State and
// the WeChat channel are fakes that record what they were asked.

function ownerMediaWorld({ requester, audience = { kind: 'owner' } } = {}) {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const state = [], sent = [], files = [], deliveries = [];
  // The steward conversation's sources: msg-w came from WeChat; dash-1 (the dashboard) has none.
  const sources = new Map([['msg-w', { direction: 'in', channel: 'wechat', sender_ref: 'wxid_owner', channel_message_ref: 'q1' }]]);
  const host = { relay: async () => ({}), bind: async () => ({ bound: false }), sourceOf: async id => sources.get(id) ?? null,
    recordDelivery: async delivery => { deliveries.push(delivery); return {}; },
    list: async () => ({ channels: [{ channel: 'wechat', bound: true, sender_ref: 'wxid_owner', revoked_at: null }], default_channel: 'wechat' }) };
  const channels = createOwnerChannels({ host });
  channels.service.register({ name: 'wechat', status: () => ({ connected: true }), mediaKinds: ['image', 'file'],
    send: async (text, options) => { sent.push([text, options]); return { ref: 'out' }; },
    sendFile: async file => { files.push(file); return { ref: 'file-out' }; } });
  channels.ready();
  const request = async (path, body) => {
    state.push([path, body]);
    if (path === '/api/state/commands/send_user_message') return { result: { message_id: `sm-${state.length}` } };
    if (path === '/api/state/commands/post_group_media') return { result: body.check_only ? { may_post: true }
      : { posted: true, message_id: 'gm1', channel: 'wechat', group_ref: 'r1' } };
    throw new Error(`unexpected ${path}`);
  };
  const media = mediaOps({ request, library, gatewayFetch: async () => { throw new Error('no gateway here'); },
    owner: { request, project: 'aitelier-home', mirror: (...args) => channels.ownerMessageSent(...args) } });
  const tools = new Map(), turn = { audience, turn: 't1' };
  registerMediaTools({ tools: { register: tool => tools.set(tool.name, tool) } }, { ctx: {}, defineTool: tool => tool, jsonOutput: {},
    HarnessError: Error, media, catalog: { rootSession: 'root', requester, catalog: () => new Set(), outward: () => false },
    turns: { audience: () => turn.audience, open: () => turn }, channels, ledger: { lookup: () => undefined } });
  const run = args => tools.get('send_media').execute(args, { agent: { session: { id: 'root' } }, callId: 'c1', token: 'tok', signal: AbortSignal.timeout(5000) });
  const messages = () => state.filter(([path]) => path === '/api/state/commands/send_user_message').map(([, body]) => body);
  return { library, state, sent, files, deliveries, media, run, messages };
}

test('24.2b send_media, dashboard turn: exactly one send_user_message with the attachment, replying to the owner\'s message; no channel send', async () => {
  const w = ownerMediaWorld({ requester: 'dash-1' });
  const { ref } = await w.library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const answer = await w.run({ ref, caption: '一只猫' });
  assert.equal(answer.sent, true); assert.equal(answer.ref, ref); assert.match(answer.message_id, /^sm-/);
  assert.deepEqual(answer.mirror, { mirrored: false, reason: 'dashboard' });
  const [message, ...more] = w.messages();
  assert.equal(more.length, 0, 'one message');
  assert.deepEqual(message.attachments, [ref]);
  assert.equal(message.body, '一只猫'); assert.equal(message.reply_to_message_id, 'dash-1'); assert.equal(message.project_id, 'aitelier-home');
  assert.equal(message.subject, 'Image');
  assert.match(message.request_key, /^send-media:/);
  assert.equal(w.sent.length + w.files.length, 0, 'nothing on a channel');
  assert.equal(w.deliveries.length, 0);
});

test('24.2b send_media, WeChat turn: the same one message, and the mirror sends the file exactly once (one delivery record)', async () => {
  const w = ownerMediaWorld({ requester: 'msg-w' });
  const { ref } = await w.library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const answer = await w.run({ ref });
  assert.equal(answer.sent, true);
  assert.deepEqual(answer.mirror, { mirrored: true, channel: 'wechat' });
  assert.equal(w.messages().length, 1);
  assert.deepEqual(w.messages()[0].attachments, [ref]); assert.equal(w.messages()[0].reply_to_message_id, 'msg-w');
  assert.equal(w.messages()[0].body, '', 'no caption: a message of the file alone');
  assert.equal(w.files.length, 1, 'sendFile exactly once');
  assert.equal(w.sent.length, 0, 'no text to send');
  const row = w.library.lookup(ref);
  assert.deepEqual(w.files[0], { kind: 'image', mime: 'image/png', name: `image-${ref.slice(2, 10)}.png`, path: row.path, to: 'wxid_owner', replyToRef: 'q1' });
  assert.equal(w.deliveries.length, 1);
  assert.equal(w.deliveries[0].messageId, answer.message_id);
  // With a caption the text goes first, then the file - still one record.
  const again = await w.run({ ref, caption: '猫' });
  assert.equal(w.sent.length, 1); assert.deepEqual(w.sent[0], ['猫', { replyToRef: 'q1', to: 'wxid_owner' }]);
  assert.equal(w.files.length, 2);
  assert.equal(w.deliveries.length, 2); assert.equal(w.deliveries[1].messageId, again.message_id);
});

test('24.2b send_media, an owner turn no owner message opened (a reminder): the message has no reply_to and goes to the default channel', async () => {
  const w = ownerMediaWorld({ requester: undefined });
  const { ref } = await w.library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const answer = await w.run({ ref });
  assert.equal(answer.sent, true, 'not refused no_requester');
  assert.equal(w.messages().length, 1);
  assert.ok(!('reply_to_message_id' in w.messages()[0]));
  assert.deepEqual(answer.mirror, { mirrored: true, channel: 'wechat' });
  assert.equal(w.files.length, 1); assert.equal(w.files[0].to, 'wxid_owner'); assert.ok(!('replyToRef' in w.files[0]));
});

test('24.2b send_media, group turn: post_group_media and the group\'s sendFile as in 24.2; no send_user_message', async () => {
  const w = ownerMediaWorld({ requester: 'msg-w', audience: { kind: 'group', channel: 'wechat', group_ref: 'r1' } });
  const { ref } = await w.library.ingest({ channel: 'generated', audience: 'group:wechat:r1', source: 'generated' }, async () => PNG);
  const answer = await w.run({ ref, caption: '猫' });
  assert.deepEqual(answer, { sent: true, ref, kind: 'image', channel: 'wechat' });
  assert.deepEqual(w.state.map(([path, body]) => [path, body.check_only === true]),
    [['/api/state/commands/post_group_media', true], ['/api/state/commands/post_group_media', false]]);
  assert.equal(w.messages().length, 0, 'never written into the owner\'s conversation');
  assert.equal(w.files.length, 1); assert.equal(w.files[0].groupRef, 'r1'); assert.equal(w.files[0].to, undefined);
  assert.equal(w.deliveries.length, 0);
});

test('24.2b sendMediaToOwner: a State refusal is {refused} and nothing is mirrored; only the steward\'s host has it', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const { ref } = await library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const mirrored = [];
  const ops = mediaOps({ request: async () => ({}), library, gatewayFetch: async () => {},
    owner: { project: 'aitelier-home', mirror: async (...args) => { mirrored.push(args); return { mirrored: true }; },
      request: async () => { throw new Error('AItelier /api/state/commands/send_user_message: HTTP 422 "invalid_attachment: each attachment"'); } } });
  assert.equal((await ops.sendToOwner({ ref })).refused, 'invalid_attachment');
  assert.equal((await ops.sendToOwner({ ref: 'm_' + '0'.repeat(16) })).refused, 'unknown_ref');
  assert.equal(mirrored.length, 0);
  assert.equal(mediaOps({ request: async () => ({}), library, gatewayFetch: async () => {} }).sendToOwner, undefined, 'no owner, no sendToOwner');
  const root = mkdtempSync(join(tmpdir(), 'steward-media-'));
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: root }));
  assert.equal(typeof steward.media.sendToOwner, 'function');
  assert.doesNotThrow(() => validateHost(steward));
  assert.equal(aitelierHost(env({ AITELIER_MEDIA_ROOT: root })).media.sendToOwner, undefined, 'a director has no owner turn');
});

test('24.2b C1: send_media in an owner turn, then the turn\'s final text: two steward messages - the file does not swallow the reply', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const { ref } = await library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  // State as the backend keeps it: the steward's messages under its token's driver id, with
  // their attachments in the conversation read.
  const conversation = [{ message_id: 'own-1', created_at: '2026-11-23T10:00:00Z', sender_kind: 'user', sender_driver_id: null,
    subject: '画只猫', body: '画只猫', segments: [] }];
  const request = async (path, body) => {
    if (path === '/api/state/query/list_user_conversation') return { messages: conversation, next_cursor: null };
    if (path !== '/api/state/commands/send_user_message') throw new Error(`unexpected ${path}`);
    const id = `st-${conversation.length}`;
    conversation.push({ message_id: id, created_at: `2026-11-23T10:00:0${conversation.length}Z`, sender_kind: 'driver',
      sender_driver_id: 'drv-home', subject: body.subject, body: body.body, segments: [], reply_to_message_id: body.reply_to_message_id ?? null,
      ...(body.attachments ? { attachments: body.attachments.map(id => ({ id, kind: 'image', mime: 'image/png', size: PNG.length, name: 'x.png' })) } : {}) });
    return { result: { message_id: id } };
  };
  const ops = mediaOps({ request, library, gatewayFetch: async () => {}, owner: { request, project: 'aitelier-home', mirror: async () => ({ mirrored: false, reason: 'dashboard' }) } });
  const sent = await ops.sendToOwner({ ref, replyTo: 'own-1' });
  assert.equal(sent.message_id, 'st-1');
  const base = { request, project: 'aitelier-home', me: 'drv-home' };
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), ['own-1'], 'the file alone answers nothing');
  const auto = await forwardFinalText({ ...base, text: '画好了，一只橘猫。', input: 'own-1' });
  assert.deepEqual(auto, { forwarded: true, reply_to: 'own-1', message_id: 'st-2' });
  const mine = conversation.filter(m => m.sender_driver_id === 'drv-home');
  assert.deepEqual(mine.map(m => [m.subject, m.body, Boolean(m.attachments)]), [['Image', '', true], ['Re: 画只猫', '画好了，一只橘猫。', false]]);
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), [], 'the text reply answers it');
});

test('24.2b F4: a captioned send_media answers the owner when the turn ends with no text; the final text, when there is one, still goes out', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const { ref } = await library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => PNG);
  const conversation = [{ message_id: 'own-1', created_at: '2026-11-23T10:00:00Z', sender_kind: 'user', sender_driver_id: null,
    subject: '画只猫', body: '画只猫', segments: [] }];
  const request = async (path, body) => {
    if (path === '/api/state/query/list_user_conversation') return { messages: conversation, next_cursor: null };
    if (path !== '/api/state/commands/send_user_message') throw new Error(`unexpected ${path}`);
    const id = `st-${conversation.length}`;
    conversation.push({ message_id: id, created_at: `2026-11-23T10:00:0${conversation.length}Z`, sender_kind: 'driver',
      sender_driver_id: 'drv-home', subject: body.subject, body: body.body, segments: [], reply_to_message_id: body.reply_to_message_id ?? null,
      ...(body.attachments ? { attachments: body.attachments.map(id => ({ id, kind: 'image', mime: 'image/png', size: PNG.length, name: 'x.png' })) } : {}) });
    return { result: { message_id: id } };
  };
  const ops = mediaOps({ request, library, gatewayFetch: async () => {}, owner: { request, project: 'aitelier-home', mirror: async () => ({ mirrored: false, reason: 'dashboard' }) } });
  const base = { request, project: 'aitelier-home', me: 'drv-home' };
  assert.equal((await ops.sendToOwner({ ref, caption: '   ', replyTo: 'own-1' })).message_id, 'st-1');
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), ['own-1'], 'a blank caption answers nothing');
  assert.equal((await ops.sendToOwner({ ref, caption: '画好了，一只橘猫。', replyTo: 'own-1' })).message_id, 'st-2');
  // The turn ended with no text: the captioned file is the answer (open threads, heldWork, idle_dormant).
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), [], 'the captioned file answers it');
  // The turn ended WITH text: forwardFinalText still sends it (C1), the file does not swallow it.
  const auto = await forwardFinalText({ ...base, text: '还要别的颜色吗？', input: 'own-1' });
  assert.deepEqual(auto, { forwarded: true, reply_to: 'own-1', message_id: 'st-3' });
  assert.deepEqual((await unansweredOwnerMessages({ ...base, mediaAnswers: false })).map(m => m.message_id), []);
});

test('design/steward-group-media.md: the adapter\'s kept media - purgeKept and keptAudiences are the library\'s, withdraw takes a kept row', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const ops = mediaOps({ request: async () => ({}), library, gatewayFetch: async () => {} });
  const meta = { channel: 'wechat', messageRef: 'k1', index: 0, audience: 'group:wechat:r1', source: 'in', kept: true };
  const kept = await ops.ingest(meta, async () => PNG);
  assert.equal((await ops.resolve(kept.ref)).kept, true);
  assert.deepEqual(await ops.keptAudiences(), ['group:wechat:r1']);
  assert.deepEqual(await ops.purgeKept('group:wechat:r1'), { rows_before: 1, rows_after: 0, files_unlinked: 1, files_shared: 0,
    bytes_freed: PNG.length });
  assert.deepEqual(await ops.keptAudiences(), []);
  const again = await ops.ingest({ ...meta, messageRef: 'k2' }, async () => PNG);
  assert.deepEqual(ops.withdraw(again.ref), { removed: 1 });
});
