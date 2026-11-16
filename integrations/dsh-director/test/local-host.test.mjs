/** The built-in local host: the full conformance suite (it is the serverless CI
 *  gate, design §9 MF5), plus its own two tools and its once-only inbox. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { conformance } from '../conformance/index.mjs';
import { localHost } from '../hosts/local.mjs';

const roots = [];
// The local host needs nothing but its root (plugin step 3 ships its todo package).
const required = {};
process.on('exit', () => {});
async function freshRoot() {
  const root = await mkdtemp(join(tmpdir(), 'dsh-director-local-'));
  roots.push(root);
  await writeFile(join(root, 'work.json'), JSON.stringify({ items: [
    { id: 'w-done', title: 'done', state: 'completed' },
    { id: 'w-live', title: 'live', state: 'working', holder: 'director' },
  ] }));
  return root;
}
const fakeApi = () => {
  const notified = [], notifiedIds = [];
  return { notified, notifiedIds, notify: async (text, options = {}) => { notified.push(text); notifiedIds.push(options.id); }, isDisposed: () => false,
    agent: {}, ctx: { goals: { get: () => undefined, create: () => {} } } };
};

conformance('local host', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, provider: 'fixture', model: 'fixture-model', pollMs: 20 });
  let n = 0;
  return { host, api: fakeApi(), seed: { settled: 'w-done', active: 'w-live', held: 'w-live',
    event: () => writeFile(join(root, 'inbox', `e${++n}.json`), JSON.stringify({ n })) } };
});

test('local host needs only a root: its role table and todo CLI default to the plugin\'s own', async () => {
  assert.throws(() => localHost({}), /missing config: root/);
  const { rolesFile, todo } = localHost({ root: '/tmp/x' }).config;
  const { loadRoles } = await import('../lib/roles.mjs');
  assert.equal(loadRoles(rolesFile).director.prompt, 'director.md');
  assert.deepEqual({ module: todo.module, stateDirEnv: todo.stateDirEnv }, { module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' });
  const { statSync } = await import('node:fs');
  assert.ok(statSync(todo.cwd + '/dsh_todo/__main__.py').isFile(), 'the default todo cwd holds the dsh_todo package');
});

test('local host: an inbox file is delivered once, and a restart does not deliver it again', async () => {
  const root = await freshRoot();
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'a.json'), '{"hello":1}');
  const first = fakeApi();
  let events = await localHost({ root, ...required, pollMs: 20 }).events(first);
  events.stop(); await events.done;
  assert.equal(first.notified.length, 1);
  assert.match(first.notified[0], /Director event \(a\.json\):\n\{"hello":1\}/);
  assert.deepEqual(await readdir(join(root, 'inbox', 'done')), ['a.json']);
  const second = fakeApi();
  events = await localHost({ root, ...required, pollMs: 20 }).events(second);
  events.stop(); await events.done;
  assert.equal(second.notified.length, 0, 'delivered again after a restart');
});

test('local host: work_update and note_write record what the director decides, and the snapshot shows it', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required });
  const tools = new Map();
  const scope = { tools: { register: tool => tools.set(tool.name, tool) } };
  await host.setup(scope, {}, { defineTool: tool => tool, jsonOutput: {} });
  assert.deepEqual([...tools.keys()], ['work_update', 'note_write']);
  assert.deepEqual(await tools.get('work_update').execute({ id: 'w-live', state: 'done' }), { refused: 'invalid_state',
    reason: 'state must be one of working, input_required, completed, failed, cancelled' });
  await tools.get('work_update').execute({ id: 'w-live', state: 'completed', holder: '' });
  await tools.get('work_update').execute({ id: 'w-new', title: 'new work', holder: 'director' });
  assert.equal(await host.workState('w-live'), 'completed');
  assert.equal(await host.workState('w-new'), 'working');
  assert.deepEqual((await host.heldWork()).map(item => item.node_key), ['w-new']);
  assert.deepEqual(await tools.get('note_write').execute({ text: '  ', request_key: 'k' }), { refused: 'empty_note', reason: 'say something' });
  assert.equal((await tools.get('note_write').execute({ text: 'no key' })).refused, 'request_key_required');
  assert.deepEqual(await tools.get('note_write').execute({ text: 'remember the release freeze', request_key: 'n1' }), { written: true });
  assert.deepEqual(await tools.get('note_write').execute({ text: 'remember the release freeze', request_key: 'n1' }), { written: false, duplicate: true });
  assert.equal((await tools.get('note_write').execute({ text: 'something else', request_key: 'n1' })).refused, 'request_key_reused');
  const snapshot = JSON.parse(await host.context('director'));
  assert.match(snapshot.notes, /remember the release freeze/);
  assert.deepEqual(snapshot.work.map(item => item.id), ['w-done', 'w-live', 'w-new']);
  await host.progress({ phase: 'dormant', subagents: [], error: '' });
  assert.match(await readFile(join(root, 'progress.jsonl'), 'utf8'), /"phase":"dormant"/);
});

test.after(async () => { for (const root of roots) await rm(root, { recursive: true, force: true }); });

test('local host: an event whose delivery failed stays in the inbox and is redelivered after a restart with the same id', async () => {
  const root = await freshRoot();
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'b.json'), '{"x":1}');
  const failedIds = [];
  const failing = { ...fakeApi(), notify: async (_text, options) => { failedIds.push(options?.id); throw new Error('crash during delivery'); } };
  await assert.rejects(localHost({ root, ...required, pollMs: 20 }).events(failing));
  assert.deepEqual(await readdir(join(root, 'inbox', 'done')), [], 'moved although it was never delivered');
  const second = fakeApi();
  const events = await localHost({ root, ...required, pollMs: 20 }).events(second);
  events.stop(); await events.done;
  assert.equal(second.notifiedIds.length, 1);
  assert.match(second.notifiedIds[0], /^inbox:b\.json:\d/);
  assert.equal(second.notifiedIds[0], failedIds[0], 'redelivered under another id');
  // A later file of the same name is a new event, with a new id.
  await writeFile(join(root, 'inbox', 'b.json'), '{"x":1}');
  const later = await localHost({ root, ...required, pollMs: 20 }).events(second);
  later.stop(); await later.done;
  assert.equal(second.notifiedIds.length, 2);
  assert.notEqual(second.notifiedIds[1], second.notifiedIds[0], 'a new file of the same name reused the old id');
  assert.deepEqual(await readdir(join(root, 'inbox', 'done')), ['b.json']);
});

test('local host: the owner answers by dropping answer-<id>.json; the event names the question, and the question reads answered', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, pollMs: 20 });
  assert.deepEqual(await host.attention.ask({ id: 'q-1', text: 'Restart now?', context: 'two workers idle' }), { asked: true, question_id: 'q-1' });
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'answer-q-1.json'), JSON.stringify({ answer: 'yes, at 14:00Z' }));
  const api = fakeApi();
  const events = await host.events(api);
  events.stop(); await events.done;
  assert.equal(api.notified.length, 1);
  const delivered = JSON.parse(api.notified[0].slice(api.notified[0].indexOf('\n') + 1));
  assert.deepEqual(delivered, { question_id: 'q-1', question: 'Restart now?', answer: 'yes, at 14:00Z' });
  assert.match(api.notifiedIds[0], /^inbox:answer-q-1\.json:/);
  const stored = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'];
  assert.equal(stored.status, 'answered'); assert.equal(stored.answer, 'yes, at 14:00Z');
  assert.deepEqual(await host.attention.list(), []);
  assert.equal((await host.attention.close({ id: 'q-1', reason: 'late' })).refused, 'question_answered', 'an answered question was withdrawn');
});

test('local host: two asks in one turn, concurrently, both land (questions.json is written one at a time)', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required });
  const answers = await Promise.all(['a', 'b', 'c', 'd'].map(id => host.attention.ask({ id: 'q-' + id, text: 'question ' + id })));
  assert.deepEqual(answers.map(a => a.asked), [true, true, true, true]);
  assert.deepEqual((await host.attention.list()).map(q => q.id).sort(), ['q-a', 'q-b', 'q-c', 'q-d']);
});

test('local host: a slow delivery is not overlapped by the next poll (one delivery per file, no move race)', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, pollMs: 5 });
  const api = fakeApi();
  const slow = { ...api, notify: async (text, options) => { await new Promise(resolve => setTimeout(resolve, 60)); return api.notify(text, options); } };
  const events = await host.events(slow);
  let failed;
  Promise.resolve(events.done).catch(error => { failed = error; });
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'slow.json'), '{"x":1}');
  const deadline = Date.now() + 3000;
  while (api.notified.length === 0 && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  await new Promise(resolve => setTimeout(resolve, 200));
  events.stop(); await Promise.resolve(events.done).catch(() => {});
  assert.equal(failed, undefined, String(failed));
  assert.equal(api.notified.length, 1, 'the file was delivered twice');
});
