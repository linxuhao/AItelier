/** Step 24.2b (design-steward 7.8.12a "宿主入库", "收图的路线", "不收图的路线", "语音与文件",
 *  "导演与管家的不同"; 7.8.18 取暂存与ack, 适配器与插件): the owner's dashboard uploads reach a
 *  host's wake. The host stores each one into its own media library and acknowledges it BEFORE
 *  the wake goes out; on an image route the image becomes a native block of the wake (and the
 *  next completion carries it as image_url), otherwise the wake's line names the ref and says
 *  to use media_call. A transient failure (the fetch answers 5xx, the network fails) does not
 *  consume the tick; a permanent one (404/410) is acknowledged `gone` and the wake goes out.
 *
 *  The first half drives steward-host.mjs's pieces with a real library and a fake backend; the
 *  second boots the real pinned DSH with the real adapter against a fake State server. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { ingestDashboardAttachments, mediaLine, uploadBody, wakeMedia, withAttachmentLines } from './steward-host.mjs';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';
import { watchProgress } from './progress.mjs';

const ONE_PIXEL_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
const png = () => Buffer.from(ONE_PIXEL_PNG, 'base64');
/** A PNG whose header says 8000 x 5125 (41 MP): the library reads the size from the header. */
const bigPng = () => { const bytes = png(); bytes.writeUInt32BE(8000, 16); bytes.writeUInt32BE(5125, 20); return bytes; };
const wav = () => Buffer.concat([Buffer.from('52494646240000005741564566', 'hex'), Buffer.alloc(64)]);
/** An ffprobe that measures every file as 23.4 s of audio. */
const fakeProbe = dir => {
  const path = join(dir, 'ffprobe');
  writeFileSync(path, `#!/bin/sh\ncat <<'EOF'\n${JSON.stringify({ streams: [{ codec_type: 'audio', disposition: { attached_pic: 0 } }], format: { duration: '23.4' } })}\nEOF\n`);
  chmodSync(path, 0o755);
  return path;
};
const REF = /m_[0-9a-f]{16}/;

/** One owner message from the dashboard, as list_driver_messages answers it (P4's shape). */
const ownerMessage = (message_id, files) => ({ message_id, sender_kind: 'user', status: 'unread', body: '看看这些',
  attachments: files.map(([id, kind, mime, size]) => ({ id, kind, mime, size, name: `${id}.bin` })) });

function rig({ answers = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'media-ingest-'));
  const library = createMediaLibrary({ root: dir, userId: 'd-drv', ffprobe: fakeProbe(dir) });
  const fetched = [], acks = [];
  const fetchUpload = async (id, signal) => {
    fetched.push(id);
    const answer = answers[id]?.shift?.() ?? answers[id];
    if (answer instanceof Error) throw answer;
    return uploadBody(new Response(answer.bytes ?? null, { status: answer.status ?? 200 }));
  };
  const ack = async (id, verdict) => { acks.push([id, verdict]); };
  return { dir, library, fetched, acks, run: inbox => ingestDashboardAttachments({ inbox, library, audience: 'project:alpha', fetchUpload, ack }) };
}

test('a dashboard image is stored under (dashboard, message, index) and acked with its ref; a voice is acked and its line says transcribe', async () => {
  const r = rig({ answers: { u_0000000000000001: { bytes: png() }, u_0000000000000002: { bytes: wav() } } });
  const inbox = [ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 70], ['u_0000000000000002', 'voice', 'audio/wav', 77]]),
    { message_id: 'd-1', sender_kind: 'driver', body: 'not the owner', attachments: [{ id: 'u_0000000000000009', kind: 'image' }] }];
  const results = await r.run(inbox);
  assert.deepEqual(r.fetched, ['u_0000000000000001', 'u_0000000000000002'], 'only the owner\'s uploads are fetched');
  const [image, voice] = results.get('m-1');
  assert.match(image.ref, REF); assert.equal(image.kind, 'image'); assert.equal(image.mime, 'image/png');
  assert.equal(voice.kind, 'voice'); assert.equal(voice.durationMs, 23400);
  assert.deepEqual(r.acks, [['u_0000000000000001', { ref: image.ref }], ['u_0000000000000002', { ref: voice.ref }]]);
  const row = r.library.rows.find(x => x.ref === image.ref);
  assert.deepEqual([row.channel, row.messageRef, row.index, row.audience, row.source], ['dashboard', 'm-1', 0, 'project:alpha', 'in']);
  assert.equal(mediaLine(voice), `[语音 ${voice.ref}，0:23；用 media_call 调 transcribe_audio 转写]`);
});

test('fetch 503 or a network error: thrown (the tick is not consumed), nothing acked; the retry stores once and a replay stores nothing again', async () => {
  const r = rig({ answers: { u_0000000000000001: [{ status: 503 }, new TypeError('fetch failed'), { bytes: png() }] } });
  const inbox = [ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 70]])];
  await assert.rejects(r.run(inbox), /unavailable/);
  await assert.rejects(r.run(inbox), /unavailable/);
  assert.deepEqual(r.acks, []);
  assert.equal(r.library.rows.length, 0);
  const first = await r.run(inbox);
  const again = await r.run(inbox);
  assert.equal(r.fetched.length, 3, 'the replay fetched nothing');
  assert.equal(again.get('m-1')[0].ref, first.get('m-1')[0].ref);
  assert.equal(r.library.rows.length, 1, 'one index row');
  assert.equal(readFileSync(r.library.indexFile, 'utf8').trim().split('\n').length, 1);
  assert.equal(r.acks.length, 2, 'the replay acks again (the backend answers a repeated ack unchanged)');
});

test('fetch 404 or 410: acked refused gone, the line is the refusal, nothing thrown; a 41 MP image is acked refused too_large', async () => {
  const r = rig({ answers: { u_0000000000000001: { status: 404 }, u_0000000000000002: { status: 410 }, u_0000000000000003: { bytes: bigPng() } } });
  const results = await r.run([ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 1], ['u_0000000000000002', 'file', 'application/pdf', 1],
    ['u_0000000000000003', 'image', 'image/png', 70]])]);
  assert.deepEqual(r.acks, [['u_0000000000000001', { refused: 'gone' }], ['u_0000000000000002', { refused: 'gone' }], ['u_0000000000000003', { refused: 'too_large' }]]);
  assert.deepEqual(results.get('m-1').map(file => mediaLine(file)), ['[图片 未收：已取不到]', '[文件 未收：已取不到]', '[图片 过大未收]']);
  assert.equal(r.library.rows.length, 0);
});

test('an ack the backend answers 404 is logged, not thrown; any other failed ack is thrown; an upload already refused is not fetched', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'media-ack-'));
  const library = createMediaLibrary({ root: dir, userId: 'owner' });
  const logged = [];
  const fetchUpload = async () => uploadBody(new Response(png()));
  const inbox = [ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 70]])];
  const notTarget = await ingestDashboardAttachments({ inbox, library, audience: 'owner', fetchUpload, log: line => logged.push(line),
    ack: async () => { throw Object.assign(new Error('AItelier ack: HTTP 404'), { status: 404 }); } });
  assert.match(notTarget.get('m-1')[0].ref, REF);
  assert.equal(logged.length, 1);
  await assert.rejects(ingestDashboardAttachments({ inbox, library, audience: 'owner', fetchUpload,
    ack: async () => { throw Object.assign(new Error('AItelier ack: HTTP 503'), { status: 503 }); } }), /503/);
  const swept = [{ ...inbox[0], attachments: [{ ...inbox[0].attachments[0], refused: 'gone' }] }];
  const fetches = [];
  const shown = await ingestDashboardAttachments({ inbox: swept, library, audience: 'owner', ack: async () => assert.fail('no ack'),
    fetchUpload: async id => { fetches.push(id); return png(); } });
  assert.deepEqual(fetches, []);
  assert.deepEqual(shown.get('m-1'), [{ kind: 'image', refused: 'gone' }]);
});

test('wake lines: an image route attaches each image once (原图已附); a route without images names the ref and media_call; a director line names the file', async () => {
  const r = rig({ answers: { u_0000000000000001: { bytes: png() }, u_0000000000000002: { bytes: wav() } } });
  const result = { events: [], driver_inbox: [ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 70], ['u_0000000000000002', 'voice', 'audio/wav', 77]])] };
  const dashboard = await r.run(result.driver_inbox);
  const [image] = dashboard.get('m-1');
  const saved = [];
  const attachments = { saveImage: async input => { saved.push(input); return { attachmentId: 'att-1', mediaType: input.mediaType } } };
  const noSources = async () => assert.fail('a director reads no source row');
  const seeing = wakeMedia({ library: r.library, imageRoute: true, attachments, director: true });
  const shown = await withAttachmentLines(noSources, result, undefined, { dashboard, sources: false, line: seeing.line });
  const path = r.library.lookup(image.ref).path;
  assert.ok(path.startsWith(r.dir + '/d-drv/'));
  assert.equal(shown.driver_inbox[0].attachments[0], `[图片 ${image.ref}，image/png，${png().length} B，原图已附；文件 ${path}]`);
  assert.match(shown.driver_inbox[0].attachments[1], /^\[语音 m_[0-9a-f]{16}，0:23；用 media_call 调 transcribe_audio 转写；文件 \/.+\]$/);
  assert.deepEqual(seeing.images, [{ attachmentId: 'att-1', mediaType: 'image/png' }], 'one image block, the voice is none');
  assert.equal(saved.length, 1); assert.deepEqual(Buffer.from(saved[0].data), png());
  // A route without images: no block, the line says how to read it.
  const blind = wakeMedia({ library: r.library, imageRoute: false, attachments, director: false });
  const plain = await withAttachmentLines(noSources, result, undefined, { dashboard, sources: false, line: blind.line });
  assert.equal(plain.driver_inbox[0].attachments[0], `[图片 ${image.ref}，image/png，${png().length} B；本路线不收图：用 media_call 调 ocr_image 或 vision_critique]`);
  assert.deepEqual(blind.images, []);
  assert.equal(saved.length, 1, 'nothing read for a route without images');
  // An image route whose attachment store refuses keeps the hint: never dropped silently.
  const broken = wakeMedia({ library: r.library, imageRoute: true, attachments: { saveImage: async () => { throw new Error('no store'); } } });
  assert.match(await broken.line(image), /；原图未附：用 media_call 调 ocr_image 或 vision_critique\]$/);
  assert.deepEqual(broken.images, []);
});

test('24.2b F3: before the routes are measured (declared, no native blocks) an image line says 原图未附, and a director\'s names read_image', async () => {
  const r = rig({ answers: { u_0000000000000001: { bytes: png() } } });
  const result = { events: [], driver_inbox: [ownerMessage('m-1', [['u_0000000000000001', 'image', 'image/png', 70]])] };
  const dashboard = await r.run(result.driver_inbox);
  const [image] = dashboard.get('m-1');
  const path = r.library.lookup(image.ref).path;
  const attachments = { saveImage: async () => assert.fail('nothing is attached before the routes are measured') };
  const director = wakeMedia({ library: r.library, imageRoute: false, declared: true, attachments, director: true });
  assert.equal(await director.line(image), `[图片 ${image.ref}，image/png，${png().length} B；原图未附：用 read_image 读该文件，或用 media_call 调 ocr_image 或 vision_critique；文件 ${path}]`);
  const steward = wakeMedia({ library: r.library, imageRoute: false, declared: true, attachments });
  assert.equal(await steward.line(image), `[图片 ${image.ref}，image/png，${png().length} B；原图未附：用 media_call 调 ocr_image 或 vision_critique]`);
  assert.deepEqual([...director.images, ...steward.images], []);
  // A measured text-only route still says it takes no images.
  assert.match(mediaLine(image, { declared: false }), /；本路线不收图：用 media_call 调 ocr_image 或 vision_critique\]$/);
});

test('the steward: a channel message\'s image (its source row) is attached on an image route too, merged with the dashboard\'s', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'media-steward-'));
  const library = createMediaLibrary({ root: dir, userId: 'owner' });
  const fromChat = await library.ingest({ channel: 'wechat', messageRef: 'w-1', index: 0, audience: 'owner', source: 'in' }, async () => png());
  const fromPanel = await ingestDashboardAttachments({ inbox: [ownerMessage('m-panel', [['u_0000000000000001', 'image', 'image/png', 70]])], library,
    audience: 'owner', fetchUpload: async () => uploadBody(new Response(png())), ack: async () => {} });
  const result = { events: [], driver_inbox: [{ message_id: 'm-chat', sender_kind: 'user', body: '[图片]' }, ownerMessage('m-panel', [['u_0000000000000001', 'image', 'image/png', 70]])] };
  const asked = [];
  const request = async (path, body) => { asked.push(body.message_ids);
    return { result: { sources: { 'm-chat': { channel: 'wechat', attachments: [{ kind: 'image', ref: fromChat.ref, mime: 'image/png', size: 70 }] } } } }; };
  const media = wakeMedia({ library, imageRoute: true, attachments: { saveImage: async ({ name }) => ({ attachmentId: name }) } });
  const shown = await withAttachmentLines(request, result, undefined, { dashboard: fromPanel, line: media.line });
  assert.deepEqual(asked, [['m-chat']], 'the dashboard message needs no source row');
  assert.deepEqual(shown.driver_inbox.map(m => m.attachments), [[`[图片 ${fromChat.ref}，image/png，70 B，原图已附]`],
    [`[图片 ${fromPanel.get('m-panel')[0].ref}，image/png，${png().length} B，原图已附]`]]);
  assert.equal(media.images.length, 2);
});

// ---- The real pinned DSH, the real adapter, a fake State server ----

/** Boots a director of project alpha with a media root. `wake` is the one driver inbox batch
 *  State answers until the cursor moves past it; `uploads` the staged bytes by id (a function
 *  answers per call); `routes` the backend's route-input. */
async function bootDirector(t, { routes, wake, uploads, until }) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;
  const directory = await mkdtemp(join(tmpdir(), 'dsh-media-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const media = join(directory, 'media');
  const seen = { completions: [], acks: [], fetches: [], waits: [], order: [] };
  let output = '', proc;
  const parked = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const body = raw.length ? JSON.parse(raw) : {};
    const send = (value, status = 200) => { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    const url = req.url;
    if (url === '/api/drivers/me') return send({ driver_id: 'media-fixture' });
    if (url === '/api/state/schema') return send({ operations: {} });
    if (url === '/api/dsh-drivers/alpha/route-input') return send(routes);
    if (url.endsWith('/context')) return send({ nodes: [], project_note: {}, private_note: {}, event_seq: 1 });
    if (url.endsWith('/activity') || url.endsWith('/heartbeat')) return send({ ok: true });
    if (url.endsWith('/list_director_messages')) return send({ items: [] });
    if (url.startsWith('/api/dsh-drivers/alpha/uploads/')) {
      const id = url.slice('/api/dsh-drivers/alpha/uploads/'.length);
      seen.fetches.push({ id, token: req.headers['x-aitelier-driver-token'] });
      seen.order.push('fetch ' + id);
      const answer = typeof uploads[id] === 'function' ? uploads[id]() : uploads[id];
      if (!answer?.bytes) return send({ detail: answer?.detail ?? 'unknown_upload' }, answer?.status ?? 404);
      res.setHeader('content-type', 'application/octet-stream');
      return res.end(answer.bytes);
    }
    if (url === '/api/state/commands/ack_conversation_upload') {
      seen.acks.push(body); seen.order.push('ack ' + body.upload_id);
      return send({ upload_id: body.upload_id, project_id: 'alpha', state: body.ref ? 'stored' : 'refused', waiting: 0 });
    }
    if (url === '/api/state/query/wait_for_state_change') {
      seen.waits.push(body);
      if (body.after < 2) return send({ events: [], driver_inbox: wake, next_after: 2, next_inbox_after: 7, timed_out: false });
      parked.push(res); return;
    }
    if (url.endsWith('/completion')) {
      seen.completions.push(body);
      if (JSON.stringify(body.messages).includes('AItelier runtime event')) seen.order.push('completion with the wake');
      return send({ text: 'Done.', reasoning_content: '', truncated: false, tool_calls: [], usage: { prompt_tokens: 10, completion_tokens: 2 } });
    }
    send({ detail: url }, 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([{ id: 'hmr', disabled: true }, { insert: [{ id: 'director', name: join(here, '../dsh-director/index.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] }]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'media-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'media-root',
      AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'absent'),
      AITELIER_MEDIA_ROOT: media, AITELIER_MEDIA_FFPROBE: fakeProbe(directory) } });
    proc.stdout.on('data', c => output += c); proc.stderr.on('data', c => output += c);
    const watched = await watchProgress({ what: 'the wake to reach a completion', running: () => proc.exitCode === null, milestones: until(seen) });
    assert.ok(watched.done, `${watched.message}\n${JSON.stringify(seen).slice(0, 3000)}\n${output.slice(-4000)}`);
    const index = (() => { try { return readFileSync(join(media, 'd-media-fixture', 'index.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l)); } catch { return []; } })();
    return { seen, index, media, alive: proc.exitCode === null, output };
  } finally {
    if (proc?.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
}

/** The completion that carried the wake, and the user message in it holding the event. */
const wakeOf = seen => {
  const completion = seen.completions.find(c => JSON.stringify(c.messages).includes('AItelier runtime event'));
  const message = completion?.messages.find(m => m.role === 'user' && JSON.stringify(m.content).includes('AItelier runtime event'));
  const text = typeof message?.content === 'string' ? message.content : message?.content?.find(p => p.type === 'text')?.text;
  return { completion, message, text };
};

test('real pinned DSH, image route: the upload is stored and acked before the wake; the wake has one image block and the completion carries it as image_url', async (t) => {
  const seen = await bootDirector(t, {
    routes: { root: ['text', 'image'], child: ['text', 'image'] },
    wake: [ownerMessage('m-1', [['u_00000000000000a1', 'image', 'image/png', 70], ['u_00000000000000a2', 'image', 'image/png', 70],
      ['u_00000000000000a3', 'voice', 'audio/wav', 77]])],
    uploads: { u_00000000000000a1: { bytes: png() }, u_00000000000000a2: { bytes: bigPng() }, u_00000000000000a3: { bytes: wav() } },
    until: s => [{ name: 'the wake reached a completion', met: () => Boolean(wakeOf(s).completion) }],
  });
  if (!seen) return;
  const { message, text } = wakeOf(seen.seen);
  assert.ok(seen.seen.fetches.every(f => f.token === 'fixture-only'), 'fetched with the driver token');
  assert.equal(seen.seen.acks.length, 3, 'each upload acknowledged: ' + JSON.stringify(seen.seen.acks));
  const [image, big, voice] = seen.seen.acks;
  assert.deepEqual([image.upload_id, big.upload_id, voice.upload_id], ['u_00000000000000a1', 'u_00000000000000a2', 'u_00000000000000a3']);
  assert.match(image.ref, REF); assert.equal(big.refused, 'too_large'); assert.match(voice.ref, REF);
  assert.ok(seen.seen.acks.every(a => a.project_id === 'alpha' && !('store' in a)), 'the ack names no store (the backend takes it from the token)');
  assert.ok(seen.seen.order.indexOf('ack u_00000000000000a3') < seen.seen.order.indexOf('completion with the wake'), 'acked before the wake: ' + seen.seen.order);
  const parts = Array.isArray(message.content) ? message.content.filter(p => p.type === 'image_url') : [];
  assert.equal(parts.length, 1, 'one image block: the voice and the refused image are none');
  assert.ok(parts[0].image_url.url.startsWith('data:image/png;base64,'));
  const path = join(seen.media, 'd-media-fixture');
  assert.ok(text.includes(`[图片 ${image.ref}，image/png，${png().length} B，原图已附；文件 ${path}/`), text);
  assert.ok(text.includes('[图片 过大未收]'), text);
  assert.ok(text.includes(`[语音 ${voice.ref}，0:23；用 media_call 调 transcribe_audio 转写；文件 ${path}/`), text);
  assert.ok(!text.includes('u_00000000000000a1'), 'the model sees the ref, not the upload id');
  assert.deepEqual(seen.index.map(row => [row.channel, row.messageRef, row.index, row.audience ?? row.refused]),
    [['dashboard', 'm-1', 0, 'project:alpha'], ['dashboard', 'm-1', 1, 'too_large'], ['dashboard', 'm-1', 2, 'project:alpha']],
    'stored rows, and the refusal kept by its key');
});

test('real pinned DSH, route without images: a 503 fetch leaves the tick for the next one (stored once, subscription alive); a 404 is acked gone and the cursor moves', async (t) => {
  let calls = 0;
  const seen = await bootDirector(t, {
    routes: { root: ['text'], child: ['text'] },
    wake: [ownerMessage('m-2', [['u_00000000000000b1', 'image', 'image/png', 70], ['u_00000000000000b2', 'image', 'image/png', 70]])],
    uploads: { u_00000000000000b1: () => (++calls === 1 ? { status: 503, detail: 'busy' } : { bytes: png() }), u_00000000000000b2: { status: 404 } },
    until: s => [{ name: 'the wake reached a completion', met: () => Boolean(wakeOf(s).completion) },
      { name: 'the cursor moved past the batch', met: () => s.waits.some(w => w.after === 2 && w.inbox_after === 7) }],
  });
  if (!seen) return;
  const { message, text } = wakeOf(seen.seen);
  assert.deepEqual(seen.seen.fetches.map(f => f.id), ['u_00000000000000b1', 'u_00000000000000b1', 'u_00000000000000b2'],
    'the 503 tick stopped at the first upload; the next tick fetched it again, then the second');
  assert.ok(seen.seen.waits.filter(w => w.after === 1).length >= 2, 'the batch was asked for again: the failed tick was not consumed');
  assert.equal(seen.seen.acks.length, 2);
  const [stored, gone] = seen.seen.acks;
  assert.equal(stored.upload_id, 'u_00000000000000b1'); assert.match(stored.ref, REF);
  assert.deepEqual(gone, { project_id: 'alpha', upload_id: 'u_00000000000000b2', refused: 'gone' });
  assert.equal(seen.index.filter(row => row.ref).length, 1, 'stored once');
  assert.equal(JSON.stringify(seen.seen.completions).includes('image_url'), false, 'no image reached the provider');
  assert.ok(typeof message.content === 'string', 'no image block');
  assert.ok(text.includes(`[图片 ${stored.ref}，image/png，${png().length} B；本路线不收图：用 media_call 调 ocr_image 或 vision_critique；文件 `), text);
  assert.ok(text.includes('[图片 未收：已取不到]'), text);
  assert.equal(seen.alive, true, 'the subscription survived the failed tick');
});

test('real pinned DSH, root route takes images but the child route does not (C2): no image block, the line says media_call', async (t) => {
  const seen = await bootDirector(t, {
    routes: { root: ['text', 'image'], child: ['text'] },
    wake: [ownerMessage('m-3', [['u_00000000000000c1', 'image', 'image/png', 70]])],
    uploads: { u_00000000000000c1: { bytes: png() } },
    until: s => [{ name: 'the wake reached a completion', met: () => Boolean(wakeOf(s).completion) }],
  });
  if (!seen) return;
  const { message, text } = wakeOf(seen.seen);
  const [stored] = seen.seen.acks;
  assert.match(stored.ref, REF);
  assert.ok(typeof message.content === 'string', 'no image block: ' + JSON.stringify(message.content).slice(0, 400));
  assert.ok(!text.includes('原图已附') && !text.includes('image omitted'), text);
  assert.ok(text.includes(`[图片 ${stored.ref}，image/png，${png().length} B；本路线不收图：用 media_call 调 ocr_image 或 vision_critique；文件 `), text);
});

test('real pinned DSH, a table with no `_input` (measured: false): the wake image stays a placeholder line', async (t) => {
  const seen = await bootDirector(t, {
    routes: { root: ['text'], child: ['text'], measured: false },
    wake: [ownerMessage('m-4', [['u_00000000000000d1', 'image', 'image/png', 70]])],
    uploads: { u_00000000000000d1: { bytes: png() } },
    until: s => [{ name: 'the wake reached a completion', met: () => Boolean(wakeOf(s).completion) }],
  });
  if (!seen) return;
  const { message, text } = wakeOf(seen.seen);
  const [stored] = seen.seen.acks;
  assert.ok(typeof message.content === 'string', 'no image block');
  // F3: the route is still declared image-capable, so the line does not say it takes no images.
  assert.ok(text.includes(`[图片 ${stored.ref}，image/png，${png().length} B；原图未附：用 read_image 读该文件，或用 media_call 调 ocr_image 或 vision_critique；文件 `), text);
});
