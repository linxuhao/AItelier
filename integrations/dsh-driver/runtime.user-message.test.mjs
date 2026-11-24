/**
 * The native driver tool, end to end: a real pinned DSH boots this driver, the
 * model calls `send_user_message`, and the tool must POST the durable
 * driver -> user action with the project supplied and nothing else invented.
 *
 * It is separate from runtime.smoke.test.mjs because that test asserts the
 * whole resident lifecycle; this one asserts one tool reaches one real door.
 *
 * Step 24.2b (design-steward 7.8.12a "导演给业主发媒体", pending 19; 7.8.18 导演给业主发媒体
 * 适配器): a director's `attachments` (absolute paths) are checked and stored in its own library
 * before the message goes; any refusal refuses the whole message and nothing reaches the
 * backend. And the director's media_call (7.8.12a "导演与管家的不同"): its own few tools, its own
 * project's refs.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, truncate } from 'node:fs/promises';
import fs, { mkdtempSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { mock } from 'node:test';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';
import { aitelierHost } from './runtime.mjs';
import { ingestDirectorAttachments, ownerLanguageSetting, sendUserMessageTool, DIRECTOR_ATTACHMENTS_SENTENCE } from './steward-host.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { registerDirectorMediaCall } from '../dsh-director/lib/media-tools.mjs';
import { DIRECTOR_MEDIA_TOOLS } from '../dsh-director/index.mjs';

const PROVIDER = `
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-provider';
export const inject = ['llm'];
export function apply(ctx) {
  let calls = 0;
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      await fetch(process.env.AITELIER_URL + '/test/turn', { method: 'POST', body: JSON.stringify(options.messages) });
      const call = calls++;
      const block = call === 0
        ? { type: 'tool-call', id: 'fixture-user-message', name: 'send_user_message',
            arguments: process.env.AITELIER_FIXTURE_TOOL_ARGS || JSON.stringify({ request_key: 'smoke-user-message', subject: 'Smoke report', body: 'durable body', node_key: 'n' }) }
        : call === 1 ? { type: 'text', text: 'Reported to the user.' }
        : {type:'text',text:'No further work.'};
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 10 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
}
`;

test('native send_user_message tool calls the durable driver -> user action', { timeout: 45000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-user-message-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const sent = [], turns = [], activity = [];
  let output = '', proc;
  const message = { message_id: 'fixture-message-id', project_id: 'alpha',
    sender_driver_id: 'dsh-fixture', created_at: '2026-11-03T10:00:00+00:00' };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture',
      event_seq: 1, nodes: [], project_note: { index: [] }, private_note: { index: [] },
      driver_skill: 'fixture driver skill', runtime_skill: 'Automatic notifications.' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/commands/send_user_message') { sent.push(body); return send(message); }
    if (req.url === '/api/state/query/wait_for_state_change') return;   // park the subscription
    if (req.url === '/test/turn') { turns.push(body); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    const provider = join(directory, 'fixture-provider.mjs');
    await writeFile(provider, PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: provider }, { id: 'director', name: join(here, '../dsh-director/index.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets') } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline && proc.exitCode === null) {
      if (sent.length && turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('fixture-message-id')))) break;
      if (activity.some(a => a.phase === 'failed')) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(proc.exitCode, null, output);
    assert.equal(sent.length, 1, 'the native tool never called the action\n' + output + JSON.stringify({ sent, activity }));
    assert.deepEqual(sent[0], { request_key: 'smoke-user-message', subject: 'Smoke report',
      body: 'durable body', project_id: 'alpha', refs: { node_key: 'n' } });
    // The identity the action returned reached the model as the tool result.
    assert.ok(turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('fixture-message-id'))),
      'the durable message identity did not reach the model\n' + JSON.stringify(turns.at(-1)).slice(0, 2000));
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity) + output);
  } finally {
    proc?.kill('SIGKILL');
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
});

// The same real driver, with the model's first tool call set to `toolArgs`.
// It reports what reached the State command and what the model got back, so a
// case can assert on the request body or on the refusal the tool returned.
// Step 24.2b: `media` gives the driver a media root (the director's own library under it);
// `toolArgs` may then be a function of the scratch directory (to name files made there), and the
// result carries the library's index lines.
async function driveSendUserMessage(t, toolArgs, { media = false } = {}) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-user-message-'));
  if (typeof toolArgs === 'function') toolArgs = await toolArgs(directory);
  const here = dirname(fileURLToPath(import.meta.url));
  const sent = [], turns = [], activity = [];
  let output = '', proc;
  const message = { message_id: 'fixture-message-id', project_id: 'alpha',
    sender_driver_id: 'dsh-fixture', created_at: '2026-11-03T10:00:00+00:00' };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture',
      event_seq: 1, nodes: [], project_note: { index: [] }, private_note: { index: [] },
      driver_skill: 'fixture driver skill', runtime_skill: 'Automatic notifications.' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/commands/send_user_message') { sent.push(body); return send(message); }
    if (req.url === '/api/state/query/wait_for_state_change') return;   // park the subscription
    if (req.url === '/test/turn') { turns.push(body); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    const provider = join(directory, 'fixture-provider.mjs');
    await writeFile(provider, PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: provider }, { id: 'director', name: join(here, '../dsh-director/index.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_FIXTURE_TOOL_ARGS: JSON.stringify(toolArgs),
      ...(media ? { AITELIER_MEDIA_ROOT: join(directory, 'media') } : {}),
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets') } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline && proc.exitCode === null) {
      // Stop once the model has its tool result (a sent body, or a refusal that reached the turn).
      if (turns.some(turn => turn.some(m => m.role === 'tool'))) break;
      if (activity.some(a => a.phase === 'failed')) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const index = media ? (await readFile(join(directory, 'media', 'd-dsh-fixture', 'index.jsonl'), 'utf8').catch(() => ''))
      .split('\n').filter(Boolean).map(line => JSON.parse(line)) : undefined;
    return { sent, turns, activity, output, exitCode: proc.exitCode, index };
  } finally {
    proc?.kill('SIGKILL');
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
}

test('native send_user_message forwards tagged segments and the reply link to the State command', { timeout: 60000 }, async (t) => {
  const segments = [{ kind: 'info', text: 'restart is coordinated' }, { kind: 'question', text: 'may I restart now?' }];
  const result = await driveSendUserMessage(t, { request_key: 'segmented', subject: 'Restart', body: 'see below',
    node_key: 'n', segments, reply_to_message_id: 'user-msg-1' });
  if (!result) return;
  assert.equal(result.exitCode, null, result.output);
  assert.equal(result.sent.length, 1, 'the native tool never reached the State command\n' + result.output + JSON.stringify(result.activity));
  // Both fields reach the command body exactly as the driver wrote them; the
  // tool adds only the project and the node ref, and invents no authority.
  assert.deepEqual(result.sent[0], { request_key: 'segmented', subject: 'Restart', body: 'see below',
    project_id: 'alpha', refs: { node_key: 'n' }, segments, reply_to_message_id: 'user-msg-1' });
});

// A question block is citable only through its id: State skips a question
// segment that carries none, and the guide tells the driver to set a stable
// one. So a caller id must reach the command as written, while a block that
// came without one must post no id key at all - the service assigns it there,
// and inventing a key here would be the tool answering a question it cannot see.
test('native send_user_message forwards a segment id and posts none for a block without one', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, { request_key: 'cited', subject: 'Restart', body: 'see below',
    segments: [{ kind: 'question', text: 'may I restart now?', id: 'restart-1' },
      { kind: 'info', text: 'restart is coordinated' }] });
  if (!result) return;
  assert.equal(result.exitCode, null, result.output);
  assert.equal(result.sent.length, 1, 'the native tool never reached the State command\n' + result.output + JSON.stringify(result.activity));
  const segments = result.sent[0].segments;
  assert.equal(segments.length, 2, JSON.stringify(segments));
  // The caller's id is the citation key: it is posted exactly as written.
  assert.deepEqual(segments[0], { kind: 'question', text: 'may I restart now?', id: 'restart-1' });
  // The other block posts the body it always did: no id key, not even null.
  assert.deepEqual(segments[1], { kind: 'info', text: 'restart is coordinated' });
  assert.ok(!Object.prototype.hasOwnProperty.call(segments[1], 'id'),
    'the tool invented a segment id: ' + JSON.stringify(segments));
});

test('native send_user_message refuses a segment whose kind is not info or question, and sends nothing', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, { request_key: 'bad-kind', subject: 'Restart', body: 'x',
    segments: [{ kind: 'demand', text: 'do it now' }] });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an unknown segment kind reached the State command: ' + JSON.stringify(result.sent));
  // The refusal is returned to the model as the tool's result, naming the block.
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('invalid_segments'))),
    'the refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});

// The two length bounds are the server's (2000 characters per block, 8000 in
// total) and the tool checks them locally too, so a block that could never be
// stored is refused before the request instead of arriving as a failed one.
test('native send_user_message refuses a block longer than 2000 characters, locally, and sends nothing', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, { request_key: 'too-long-block', subject: 'Restart', body: 'x',
    segments: [{ kind: 'info', text: 'y'.repeat(2000) }, { kind: 'question', text: 'z'.repeat(2001) }] });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an over-long block reached the State command: ' + JSON.stringify(result.sent).slice(0, 500));
  // The refusal is the tool's own result, and it names the block that is too long.
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool'
    && JSON.stringify(m).includes('invalid_segments') && JSON.stringify(m).includes('segments[1].text'))),
  'the length refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});

test('native send_user_message refuses blocks totalling more than 8000 characters, locally, and sends nothing', { timeout: 60000 }, async (t) => {
  // Five blocks of 1700 characters: every one is legal on its own, the total is not.
  const segments = Array.from({ length: 5 }, (_, index) => ({ kind: 'info', text: String(index).repeat(1700) }));
  const result = await driveSendUserMessage(t, { request_key: 'too-long-total', subject: 'Restart', body: 'x', segments });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an over-total message reached the State command: ' + JSON.stringify(result.sent).slice(0, 500));
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool'
    && JSON.stringify(m).includes('invalid_segments') && JSON.stringify(m).includes('8000'))),
  'the total-length refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});

// --- step 24.2b: a director's attachments and its media_call --------------------------------

/** A PNG whose header says width x height (the library reads the size from the header only). */
const png = (width = 64, height = 64) => Buffer.concat([Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex'),
  Buffer.from([width >>> 24, (width >>> 16) & 255, (width >>> 8) & 255, width & 255, height >>> 24, (height >>> 16) & 255, (height >>> 8) & 255, height & 255]),
  Buffer.alloc(40)]);
const pdf = () => Buffer.concat([Buffer.from('%PDF-1.4\n'), Buffer.alloc(80, 32)]);
const REF = /^m_[0-9a-f]{16}$/;
/** The files a director might attach, made in `dir`: their absolute paths. */
async function attachable(dir) {
  const at = name => join(dir, name);
  await writeFile(at('shot.png'), png());
  await writeFile(at('other.png'), png(32, 32));
  await writeFile(at('report.pdf'), pdf());
  await mkdir(at('a-directory'));
  // An office document with macros: a zip, which the shared table does not know.
  await writeFile(at('report.docm'), Buffer.concat([Buffer.from('504b030414000600', 'hex'), Buffer.alloc(200)]));
  // 21 MiB that start like a PNG: over the 20 MiB a single image may be.
  await writeFile(at('huge.png'), png());
  await truncate(at('huge.png'), 21 * 1024 * 1024);
  // 8000 x 5125 = 41 MP: within every byte bound, over the 40 MP an image may be.
  await writeFile(at('wide.png'), png(8000, 5125));
  return { shot: at('shot.png'), other: at('other.png'), report: at('report.pdf'), directory: at('a-directory'), docm: at('report.docm'),
    huge: at('huge.png'), wide: at('wide.png'), missing: at('no-such-file.png') };
}
const rig = async () => {
  const dir = mkdtempSync(join(tmpdir(), 'director-attach-'));
  const files = await attachable(dir);
  const root = join(dir, 'media');
  return { dir, files, root, library: createMediaLibrary({ root, userId: 'd-drv1' }) };
};
const attach = (library, requestKey, paths) => ingestDirectorAttachments({ library, project: 'alpha', requestKey, paths });

test('24.2b director attachments: a missing path, a directory, .docm, a 21 MiB PNG and a 41 MP image each answer their code; nothing is stored', async () => {
  const { files, library } = await rig();
  const cases = [[files.missing, 'not_found'], ['relative/shot.png', 'not_found'], [files.directory, 'not_a_file'], [files.docm, 'type'],
    [files.huge, 'too_large'], [files.wide, 'too_large']];
  for (const [index, [path, code]] of cases.entries())
    assert.deepEqual(await attach(library, `k-${index}`, [path]), { refused: 'attachment_refused', attachments: [{ path, refused: code }] }, path);
  assert.deepEqual(library.rows, [], 'no file was stored');
  // More than ten is the request's own fault, before any file is looked at.
  assert.equal((await attach(library, 'k-many', Array(11).fill(files.shot))).refused, 'invalid_request');
  assert.deepEqual(library.rows, []);
});

test('24.2b director attachments: one bad of three refuses the whole message and lists every refused path; nothing is stored', async () => {
  const { files, library } = await rig();
  assert.deepEqual(await attach(library, 'k1', [files.shot, files.missing, files.directory]), { refused: 'attachment_refused',
    attachments: [{ path: files.missing, refused: 'not_found' }, { path: files.directory, refused: 'not_a_file' }] });
  assert.deepEqual(library.rows, []);
  // Good files: stored in order under (director, request_key, index), audience this project, source generated.
  const ok = await attach(library, 'k2', [files.shot, files.report]);
  assert.deepEqual(ok.attachments.map(a => [a.kind, a.mime, a.name]), [['image', 'image/png', 'shot.png'], ['file', 'application/pdf', 'report.pdf']]);
  for (const a of ok.attachments) { assert.match(a.ref, REF); assert.ok(a.size > 0); }
  assert.deepEqual(library.rows.map(r => [r.ref, r.channel, r.messageRef, r.index, r.audience, r.source]),
    ok.attachments.map((a, index) => [a.ref, 'director', 'k2', index, 'project:alpha', 'generated']));
});

test('24.2b director attachments: the same key with the same file is the same ref (one row); with another file idempotency_conflict', async () => {
  const { files, library } = await rig();
  const first = await attach(library, 'k1', [files.shot]);
  const again = await attach(library, 'k1', [files.shot]);
  assert.deepEqual(again, first);
  assert.equal(library.rows.length, 1);
  assert.deepEqual(await attach(library, 'k1', [files.other]), { refused: 'attachment_refused',
    attachments: [{ path: files.other, refused: 'idempotency_conflict' }] });
  assert.equal(library.rows.length, 1, 'the old file is never sent under the new one\'s name');
});

test('24.2b director attachments: the 21st in an hour is rate_limited with retry_after, still after a restart (counted from the index)', async () => {
  const { files, root, library } = await rig();
  for (let i = 0; i < 20; i++) assert.match((await attach(library, `k-${i}`, [files.shot])).attachments[0].ref, REF);
  const refused = await attach(library, 'k-20', [files.shot]);
  assert.equal(refused.refused, 'attachment_refused');
  assert.equal(refused.attachments[0].refused, 'rate_limited');
  assert.ok(Number.isInteger(refused.attachments[0].retry_after) && refused.attachments[0].retry_after > 0, JSON.stringify(refused));
  // A new host process: a new library over the same subdirectory.
  const restarted = createMediaLibrary({ root, userId: 'd-drv1' });
  assert.equal((await attach(restarted, 'k-21', [files.shot])).attachments[0].refused, 'rate_limited');
  // A replay of a key already stored is not counted: it answers its ref.
  assert.match((await attach(restarted, 'k-3', [files.shot])).attachments[0].ref, REF);
});

test('24.2b director attachments: a write the disk cannot take is store_failed; nothing is kept', async () => {
  const { files, library } = await rig();
  const real = fs.writeFileSync;
  mock.method(fs, 'writeFileSync', function (path, ...rest) {
    if (String(path).includes('.incoming-')) throw Object.assign(new Error('ENOSPC'), { code: 'ENOSPC' });
    return real.call(this, path, ...rest);
  });
  syncBuiltinESMExports();
  try {
    assert.deepEqual(await attach(library, 'k1', [files.shot]), { refused: 'attachment_refused', attachments: [{ path: files.shot, refused: 'store_failed' }] });
  } finally { mock.restoreAll(); syncBuiltinESMExports(); }
  assert.deepEqual(library.rows, []);
  assert.match((await attach(library, 'k1', [files.shot])).attachments[0].ref, REF, 'not recorded: the same request goes through later');
});

test('24.2b director attachments: the library\'s own codes outside the documented set are mapped - unsupported is type, unavailable store_failed (nit C6)', async () => {
  const { dir, library } = await rig();
  // PNG magic bytes, but no header to read a size from: the library answers `unsupported`.
  const unreadable = join(dir, 'unreadable.png');
  await writeFile(unreadable, Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), Buffer.alloc(8)]));
  assert.deepEqual(await attach(library, 'k1', [unreadable]), { refused: 'attachment_refused', attachments: [{ path: unreadable, refused: 'type' }] });
  const { files } = await rig();
  const real = fs.writeFileSync;
  mock.method(fs, 'writeFileSync', function (path, ...rest) {
    if (String(path).includes('.incoming-')) throw Object.assign(new Error('EACCES'), { code: 'EACCES' });
    return real.call(this, path, ...rest);
  });
  syncBuiltinESMExports();
  try {
    assert.deepEqual(await attach(library, 'k2', [files.shot]), { refused: 'attachment_refused', attachments: [{ path: files.shot, refused: 'store_failed' }] });
  } finally { mock.restoreAll(); syncBuiltinESMExports(); }
});

test('24.2b director attachments: a relative path is not_found even when it names a real file in the working directory (nit TQ-3)', async () => {
  const { dir, library } = await rig();
  const before = process.cwd();
  process.chdir(dir);
  try {
    assert.deepEqual(await attach(library, 'k1', ['shot.png']), { refused: 'attachment_refused', attachments: [{ path: 'shot.png', refused: 'not_found' }] });
  } finally { process.chdir(before); }
  assert.deepEqual(library.rows, []);
});

test('24.2b send_user_message schema: only a director with a media library gets `attachments` and its one sentence', async () => {
  const { library } = await rig();
  const director = sendUserMessageTool({ steward: false, library });
  assert.deepEqual(director.parameters.attachments, { type: 'array', items: { type: 'string' } });
  assert.ok(director.description.endsWith(DIRECTOR_ATTACHMENTS_SENTENCE));
  assert.equal(DIRECTOR_ATTACHMENTS_SENTENCE.trim().split(/(?<=[.;])\s+(?=[A-Z])/).length, 1, 'one sentence');
  for (const sentence of ['30 days', 'this project', 'WeChat or Slack']) assert.ok(DIRECTOR_ATTACHMENTS_SENTENCE.includes(sentence), sentence);
  const bare = sendUserMessageTool({ steward: false, library: undefined });
  assert.ok(!('attachments' in bare.parameters)); assert.ok(!bare.description.includes(DIRECTOR_ATTACHMENTS_SENTENCE));
  assert.deepEqual(Object.keys(director.parameters).slice(0, -1), Object.keys(bare.parameters), 'every other parameter as before');
});

test('the owner\'s language: the environment\'s, else the .env line the entry names (dotenv_boot\'s grammar), else none', () => {
  const dir = mkdtempSync(join(tmpdir(), 'owner-language-')), file = join(dir, '.env');
  assert.equal(ownerLanguageSetting({}, file), undefined, 'no .env');
  fs.writeFileSync(file, '# AITELIER_OWNER_LANGUAGE=en\nAITELIER_PORT=4444\r\nexport AITELIER_OWNER_LANGUAGE="zh"\nAITELIER_OWNER_LANGUAGE=en\n');
  assert.equal(ownerLanguageSetting({}, file), 'zh', 'the first assignment, export and quotes dropped');
  assert.equal(ownerLanguageSetting({ AITELIER_OWNER_LANGUAGE: 'en' }, file), 'en', 'the environment wins');
  assert.equal(ownerLanguageSetting({ AITELIER_OWNER_LANGUAGE: '' }, file), undefined, 'set but empty: the default');
  fs.writeFileSync(file, 'AITELIER_OWNER_LANGUAGE=\n');
  assert.equal(ownerLanguageSetting({}, file), undefined);
  assert.equal(ownerLanguageSetting({}, dir), undefined, 'a directory is no .env');
  const env = { AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'm',
    AITELIER_DSH_STATE_DIR: dir, AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv1', AITELIER_DSH_ROLE: 'steward' };
  assert.equal(aitelierHost({ ...env, AITELIER_OWNER_LANGUAGE: 'zh' }).config.ownerLanguage, 'zh');
  assert.equal(aitelierHost({ ...env, AITELIER_OWNER_LANGUAGE: '' }).config.ownerLanguage, undefined);
  // Only the entry (apply) names the checkout's .env: a caller that names none - every test -
  // never reads a file, so a checkout whose .env says zh decides no test (core/dotenv_boot.py).
  fs.writeFileSync(file, 'AITELIER_OWNER_LANGUAGE=zh\n');
  assert.equal(aitelierHost(env, { dotenv: file }).config.ownerLanguage, 'zh', 'the named .env');
  assert.equal(aitelierHost({ ...env, AITELIER_OWNER_LANGUAGE: 'en' }, { dotenv: file }).config.ownerLanguage, 'en', 'the environment wins');
  assert.equal(ownerLanguageSetting({}), undefined, 'no file named: none read');
  // ...and the entry does name it: this checkout's .env, as before (the live fallback).
  assert.ok(fs.readFileSync(new URL('./runtime.mjs', import.meta.url), 'utf8')
    .includes("ctx.provide('directorHost', aitelierHost(process.env, { dotenv: new URL('../../.env', import.meta.url) }));"));
});

/** The director host's media_call, registered as index.mjs does, with a fake registry. */
function directorMediaCall({ root, registry = DIRECTOR_MEDIA_TOOLS }) {
  const host = aitelierHost({ AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'root-1', AITELIER_DSH_PROVIDER: 'aitelier',
    AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv1',
    AITELIER_DSH_EXECUTION: 'host', AITELIER_MEDIA_ROOT: root });
  const calls = [], tools = new Map();
  const ctx = { tools: { schemas: () => registry.map(name => ({ name })),
    execute: async call => { calls.push(call); return { value: { text: 'read' }, content: [{ type: 'text', text: 'read' }] }; } } };
  registerDirectorMediaCall({ tools: { register: tool => tools.set(tool.name, tool) } }, { ctx, defineTool: t => t,
    jsonOutput: {}, HarnessError: Error, media: host.media, rootSession: 'root-1', projectId: 'alpha', names: DIRECTOR_MEDIA_TOOLS });
  const run = (args, session = 'root-1') => tools.get('media_call').execute(args, { agent: { session: { id: session } }, callId: 'c1',
    rootCallId: 'c1', token: {}, signal: AbortSignal.timeout(5000) });
  return { host, calls, run, names: [...tools.keys()] };
}

test('24.2b director media_call: its own project\'s ref is filled in as base64 and runs one of its three tools', async () => {
  const root = mkdtempSync(join(tmpdir(), 'director-media-call-'));
  const d = directorMediaCall({ root });
  assert.deepEqual(d.names, ['media_call']);
  const { ref } = await d.host.media.ingest({ channel: 'dashboard', messageRef: 'msg-1', index: 0, audience: 'project:alpha', source: 'in' }, async () => png());
  assert.deepEqual(await d.run({ name: 'mcp__agentmcp__ocr_image', media: { image_base64: ref } }), { text: 'read' });
  assert.equal(d.calls.length, 1);
  assert.equal(d.calls[0].name, 'mcp__agentmcp__ocr_image');
  assert.equal(d.calls[0].arguments.image_base64, png().toString('base64'));
});

test('24.2b director media_call: another subdirectory\'s ref, another project\'s, a name outside the three, an unregistered one or a worker -> refused, nothing runs', async () => {
  const root = mkdtempSync(join(tmpdir(), 'director-media-call-'));
  const d = directorMediaCall({ root });
  // Another director's library, and the steward's, under the same root.
  const theirs = await createMediaLibrary({ root, userId: 'd-drv2' }).ingest({ channel: 'director', messageRef: 'k', index: 0, audience: 'project:alpha', source: 'generated' }, async () => png());
  const stewards = await createMediaLibrary({ root, userId: 'owner' }).ingest({ channel: 'wechat', messageRef: 'w', index: 0, audience: 'owner', source: 'in' }, async () => png());
  for (const { ref } of [theirs, stewards])
    assert.equal((await d.run({ name: 'mcp__agentmcp__ocr_image', media: { image_base64: ref } })).refused, 'unknown_ref', ref);
  // Its own library, but a row of another project.
  const elsewhere = await d.host.media.ingest({ channel: 'director', messageRef: 'k2', index: 0, audience: 'project:beta', source: 'generated' }, async () => png());
  assert.equal((await d.run({ name: 'mcp__agentmcp__ocr_image', media: { image_base64: elsewhere.ref } })).refused, 'not_visible_in_this_turn');
  const { ref } = await d.host.media.ingest({ channel: 'director', messageRef: 'k3', index: 0, audience: 'project:alpha', source: 'generated' }, async () => png());
  for (const name of ['mcp__agentmcp__ocr_pdf', 'mcp__agentmcp__generate_image', 'bash', 'call_tool'])
    assert.equal((await d.run({ name, media: { image_base64: ref } })).refused, 'not_allowed', name);
  // One of the three, but not in this session's registry.
  const unregistered = directorMediaCall({ root, registry: ['mcp__agentmcp__ocr_image'] });
  assert.equal((await unregistered.run({ name: 'mcp__agentmcp__vision_critique', media: { image_base64: ref } })).refused, 'not_registered');
  // A worker's session.
  assert.equal((await d.run({ name: 'mcp__agentmcp__ocr_image', media: { image_base64: ref } }, 'child-1')).refused, 'not_available');
  assert.deepEqual([...d.calls, ...unregistered.calls], [], 'nothing ran');
});

test('24.2b native send_user_message, director with a media root: the attached files go in as refs, the result names them', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, async dir => {
    const files = await attachable(dir);
    return { request_key: 'with-files', subject: 'Screens', body: 'see the frames', attachments: [files.shot, files.report] };
  }, { media: true });
  if (!result) return;
  assert.equal(result.exitCode, null, result.output);
  assert.equal(result.sent.length, 1, 'the message never reached the State command\n' + result.output);
  const refs = result.index.map(row => row.ref);
  assert.equal(refs.length, 2);
  assert.deepEqual(result.sent[0], { request_key: 'with-files', subject: 'Screens', body: 'see the frames', project_id: 'alpha', attachments: refs });
  assert.deepEqual(result.index.map(row => [row.channel, row.messageRef, row.index, row.audience, row.source]),
    [['director', 'with-files', 0, 'project:alpha', 'generated'], ['director', 'with-files', 1, 'project:alpha', 'generated']]);
  const tool = JSON.stringify(result.turns.flat().filter(m => m.role === 'tool'));
  for (const ref of refs) assert.ok(tool.includes(ref), 'the result names ' + ref);
  assert.ok(tool.includes('shot.png') && tool.includes('report.pdf') && tool.includes('fixture-message-id'), tool.slice(0, 1500));
});

test('24.2b native send_user_message, director with a media root: a bad attachment refuses the whole message - nothing reaches the backend', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, async dir => {
    const files = await attachable(dir);
    return { request_key: 'bad-files', subject: 'Screens', body: 'x', attachments: [files.shot, files.missing, files.directory, files.docm, files.huge] };
  }, { media: true });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'a refused message reached the State command: ' + JSON.stringify(result.sent));
  assert.deepEqual(result.index, [], 'nothing was stored');
  const tool = JSON.stringify(result.turns.flat().filter(m => m.role === 'tool'));
  assert.ok(tool.includes('attachment_refused'), tool.slice(0, 1500));
  for (const code of ['not_found', 'not_a_file', 'type', 'too_large']) assert.ok(tool.includes(code), code + ' ' + tool.slice(0, 1500));
  // An ingest refusal (41 MP) refuses the message too.
  const wide = await driveSendUserMessage(t, async dir => ({ request_key: 'wide', subject: 'S', body: 'x', attachments: [(await attachable(dir)).wide] }), { media: true });
  assert.equal(wide.sent.length, 0);
  assert.ok(JSON.stringify(wide.turns.flat().filter(m => m.role === 'tool')).includes('too_large'));
});

test('24.2b native send_user_message, director with a media root but no attachments: the request body is byte for byte today\'s', { timeout: 60000 }, async (t) => {
  for (const extra of [{}, { attachments: [] }]) {
    const result = await driveSendUserMessage(t, { request_key: 'plain', subject: 'S', body: 'b', node_key: 'n', ...extra }, { media: true });
    if (!result) return;
    assert.equal(result.sent.length, 1, result.output);
    assert.equal(JSON.stringify(result.sent[0]), JSON.stringify({ request_key: 'plain', subject: 'S', body: 'b', project_id: 'alpha', refs: { node_key: 'n' } }));
  }
});

test('24.2b native send_user_message, director without a media library: attachments are refused, never dropped - nothing reaches the backend', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, async dir => ({ request_key: 'no-library', subject: 'S', body: 'x',
    attachments: [(await attachable(dir)).shot] }));
  if (!result) return;
  assert.equal(result.sent.length, 0, 'the text went out without its file: ' + JSON.stringify(result.sent));
  assert.ok(JSON.stringify(result.turns.flat().filter(m => m.role === 'tool')).includes('attachments_unsupported'));
});
