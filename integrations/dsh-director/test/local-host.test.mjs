/** The built-in local host: the full conformance suite (it is the serverless CI
 *  gate, design §9 MF5), plus its own two tools and its once-only inbox. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { conformance } from '../conformance/index.mjs';
import { localHost, LOCAL_OWNER } from '../hosts/local.mjs';
import { PURGE_WORD } from '../hosts/local-commands.mjs';
import { validateHost } from '../lib/host.mjs';
import { MemoryStore } from '../lib/memory/store.mjs';

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
  assert.deepEqual(delivered, { question_id: 'q-1', question: 'Restart now?', answer: 'yes, at 14:00Z', answered_by: 'owner:local' });
  assert.match(api.notifiedIds[0], /^inbox:answer-q-1\.json:/);
  const stored = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'];
  assert.equal(stored.status, 'answered'); assert.equal(stored.answer, 'yes, at 14:00Z');
  // Step 15: an answer is attributable - a file that does not say who answered is the owner's (owner:local).
  assert.equal(stored.answered_by, 'owner:local');
  assert.match(stored.answered_at, /^\d{4}-\d\d-\d\dT/);
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

// ---- Plugin step 15: the standalone owner's commands, portfolio and proposals.

/** A command registry like DSH's (ctx.commands): register returns the unregister function. */
const fakeCommands = () => {
  const defs = new Map(), removed = [];
  return { defs, removed, register: def => { defs.set(def.name, def); return () => { removed.push(def.name); defs.delete(def.name); }; },
    run: async (name, rawInput = '') => defs.get(name).handler({ rawInput, agent: {}, attachments: [], signal: AbortSignal.timeout(10000) }) };
};
const commandApi = (commands, memory) => {
  const api = fakeApi();
  return { ...api, memory, ctx: { ...api.ctx, get: name => name === 'commands' ? commands : undefined } };
};
const until = async (what, met, ms = 3000) => {
  const deadline = Date.now() + ms;
  while (!(await met()) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(await met(), what);
};

test('local host, step 15: /questions and /answer - the answer is recorded as owner:local, wakes the root once, and is final', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, pollMs: 10 });
  await host.attention.ask({ id: 'q-1', text: 'Restart now?', context: 'two workers idle' });
  await host.attention.ask({ id: 'q-2', text: 'Which colour?' });
  await host.attention.ask({ id: 'q-3', text: 'Drop it?' });
  await host.attention.close({ id: 'q-3', reason: 'not needed' });
  const commands = fakeCommands();
  const api = commandApi(commands);
  const events = await host.events(api);
  // No projectsDir and no memory: the question commands only.
  assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'questions']);
  const listed = await commands.run('questions');
  assert.equal(listed.kind, 'success');
  assert.match(listed.text, /Open questions \(2\):\n- q-1: Restart now\?\n {2}context: two workers idle/);
  assert.match(listed.text, /- q-2: Which colour\?/);
  assert.doesNotMatch(listed.text, /q-3/);
  assert.equal((await commands.run('answer', ' q-1')).kind, 'error', 'an answer without text');
  assert.match((await commands.run('answer', ' q-9 yes')).text, /unknown_question/);
  assert.match((await commands.run('answer', ' __proto__ yes')).text, /unknown_question/);
  const answered = await commands.run('answer', ' q-1 yes, at 14:00Z ');
  assert.equal(answered.kind, 'success', answered.text);
  assert.match(answered.text, /Answered q-1 \(as owner:local, /);
  // questions.json says so at once, attributed.
  const stored = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'];
  assert.deepEqual([stored.status, stored.answer, stored.answered_by], ['answered', 'yes, at 14:00Z', LOCAL_OWNER]);
  // The root is woken by the inbox poll, once, naming the question and who answered.
  await until('the answer never woke the root', () => api.notified.length === 1);
  const delivered = JSON.parse(api.notified[0].slice(api.notified[0].indexOf('\n') + 1));
  assert.deepEqual(delivered, { question_id: 'q-1', question: 'Restart now?', answer: 'yes, at 14:00Z', answered_by: 'owner:local' });
  assert.match((await commands.run('answer', ' q-1 no')).text, /question_answered/, 'a question is answered once');
  // A withdrawn question can still be answered (an answer wins over a withdrawal).
  assert.equal((await commands.run('answer', ' q-3 drop it')).kind, 'success');
  assert.equal(JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-3'].status, 'answered');
  assert.doesNotMatch((await commands.run('questions')).text, /q-1|q-3/);
  await until('the second answer never woke the root', () => api.notified.length === 2);
  events.stop(); await events.done;
  assert.deepEqual(commands.removed.sort(), ['answer', 'questions'], 'stop() unregisters the commands');
  assert.equal(api.notified.length, 2);
});

test('local host, step 15: /answer writes the wake file first - a crash before questions.json still records and delivers it', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, pollMs: 10 });
  await host.attention.ask({ id: 'q-1', text: 'Restart now?' });
  // What /answer leaves when the process dies right after the answer file: the file only.
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'answer-q-1.json'), JSON.stringify({ answer: 'yes', answered_by: LOCAL_OWNER }));
  const commands = fakeCommands();
  const api = commandApi(commands);
  const events = await host.events(api);
  assert.equal(api.notified.length, 1, 'the next start delivered the answer');
  const stored = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'];
  assert.deepEqual([stored.status, stored.answer, stored.answered_by], ['answered', 'yes', 'owner:local']);
  assert.match((await commands.run('answer', ' q-1 again')).text, /question_answered/);
  events.stop(); await events.done;
});

test('local host, step 15: without a command registry nothing is registered and nothing breaks', async () => {
  const root = await freshRoot();
  const api = fakeApi();
  const events = await localHost({ root, ...required, projectsDir: join(root, 'projects'), pollMs: 10 }).events(api);
  events.stop(); await events.done;
  assert.deepEqual(localHost({ root }).capabilities, ['progress', 'workState', 'heldWork', 'setup', 'attention'],
    'no projectsDir: no portfolio, no proposals (the host is what it was)');
  assert.equal(localHost({ root }).portfolio, undefined);
});

async function projectsFixture() {
  const root = await freshRoot();
  const projectsDir = join(root, 'projects');
  await mkdir(join(projectsDir, 'alpha'), { recursive: true });
  await writeFile(join(projectsDir, 'alpha', 'project.json'), JSON.stringify({ title: 'Alpha', goal: 'ship alpha' }));
  await writeFile(join(projectsDir, 'alpha', 'work.json'), JSON.stringify({ items: [
    { id: 'a1', title: 'first', state: 'working', holder: 'director' }, { id: 'a2', title: 'second', state: 'completed' },
    { id: 'a3', title: 'third', state: 'working' }] }));
  await writeFile(join(projectsDir, 'alpha', 'questions.json'), JSON.stringify({
    'q-old': { text: 'Old?', status: 'answered', asked_at: '2026-11-01T00:00:00Z', answer: 'yes', answered_by: 'owner:local', answered_at: '2026-11-02T00:00:00Z' },
    'q-new': { text: 'New?', status: 'open', asked_at: '2026-11-03T00:00:00Z' } }));
  await writeFile(join(projectsDir, 'alpha', 'progress.jsonl'), '{"at":"2026-11-03T01:00:00Z","phase":"running"}\n{"at":"2026-11-03T02:00:00Z","phase":"dormant"}\n');
  await mkdir(join(projectsDir, 'beta'), { recursive: true });
  await mkdir(join(projectsDir, '.hidden'), { recursive: true });
  await writeFile(join(projectsDir, 'not-a-dir'), 'x');
  return { root, projectsDir };
}

conformance('local host with projectsDir', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, ...required, projectsDir, provider: 'fixture', model: 'fixture-model', pollMs: 20 });
  let n = 0;
  return { host, api: fakeApi(), seed: { settled: 'w-done', active: 'w-live', held: 'w-live',
    event: () => writeFile(join(root, 'inbox', `e${++n}.json`), JSON.stringify({ n })) } };
});

test('local host, step 15: projectsDir is the steward\'s portfolio - every subdirectory one project, read-only', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, ...required, projectsDir, provider: 'fixture', model: 'fixture-model' });
  assert.equal(validateHost(host), host);
  assert.deepEqual(host.capabilities, ['progress', 'workState', 'heldWork', 'setup', 'attention', 'portfolio', 'proposals']);
  assert.deepEqual(await host.portfolio.projects(), { projects: [
    { project_id: 'alpha', title: 'Alpha', director: { phase: 'dormant', at: '2026-11-03T02:00:00Z' }, open_questions: 1 },
    { project_id: 'beta', title: null, director: null, open_questions: 0 }] });
  const alpha = await host.portfolio.project('alpha');
  assert.deepEqual(alpha.work_counts, { working: 2, completed: 1 });
  assert.deepEqual(alpha.open_work.map(item => item.id), ['a1', 'a3']);
  assert.deepEqual(alpha.open_questions, [{ id: 'q-new', text: 'New?', asked_at: '2026-11-03T00:00:00Z' }]);
  assert.equal(alpha.goal, 'ship alpha');
  const conversation = await host.portfolio.conversation('alpha');
  assert.deepEqual(conversation.messages.map(m => m.id), ['q-new', 'q-old'], 'newest first');
  assert.equal(conversation.next_cursor, null);
  for (const id of ['gamma', '../alpha', 'not-a-dir', '.hidden'])
    assert.equal((await host.portfolio.project(id)).refused, 'unknown_project', id);
  assert.equal((await host.portfolio.conversation('gamma')).refused, 'unknown_project');
  // Nothing was written by reading.
  assert.deepEqual((await readdir(projectsDir)).sort(), ['.hidden', 'alpha', 'beta', 'not-a-dir']);
});

test('local host, step 15: proposals - create_project only; the owner approves with /approve, the project appears whole, the steward is woken', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, ...required, projectsDir, pollMs: 10 });
  assert.deepEqual(host.proposals.kinds, ['create_project']);
  const seed = [{ key: 'g1', goal: 'write the README', acceptance: [{ id: 'c1', kind: 'review', description: 'it reads well' }] }];
  const good = { project_id: 'gamma', title: 'Gamma', goal: 'try gamma', seed_nodes: seed };
  assert.equal((await host.proposals.propose({ kind: 'start_driver', payload: { project_id: 'alpha' }, reason: 'r' })).refused, 'unknown_kind');
  assert.equal((await host.proposals.propose({ kind: 'create_project', payload: { ...good, project_id: '../x' }, reason: 'r' })).refused, 'invalid_payload');
  assert.equal((await host.proposals.propose({ kind: 'create_project', payload: { ...good, seed_nodes: [{ key: 'g1', goal: 'x', acceptance: [] }] }, reason: 'r' })).refused, 'invalid_payload');
  assert.equal((await host.proposals.propose({ kind: 'create_project', payload: good, reason: ' ' })).refused, 'reason_required');
  assert.deepEqual(await host.proposals.propose({ kind: 'create_project', payload: { ...good, title: 'G' }, reason: 'the owner asked' }),
    { proposed: true, proposal_id: 'p-1', status: 'open' });
  assert.deepEqual(await host.proposals.propose({ kind: 'create_project', payload: good, reason: 'the owner asked', supersedes: 'p-1' }),
    { proposed: true, proposal_id: 'p-2', status: 'open' });
  assert.equal((await host.proposals.propose({ kind: 'create_project', payload: good, reason: 'r', supersedes: 'p-1' })).refused, 'not_open');
  await host.proposals.propose({ kind: 'create_project', payload: { ...good, project_id: 'alpha' }, reason: 'taken id' });
  await host.proposals.propose({ kind: 'create_project', payload: { ...good, project_id: 'delta' }, reason: 'to decline' });
  await host.proposals.propose({ kind: 'create_project', payload: { ...good, project_id: 'eps' }, reason: 'to withdraw' });
  assert.deepEqual(await host.proposals.withdraw({ id: 'p-5', reason: 'changed my mind' }), { withdrawn: true, proposal_id: 'p-5' });
  assert.equal((await host.proposals.withdraw({ id: 'p-5', reason: 'again' })).already_withdrawn, true);

  const commands = fakeCommands();
  const api = commandApi(commands);
  const events = await host.events(api);
  assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'approve', 'decline', 'proposals', 'questions']);
  const listed = (await commands.run('proposals')).text;
  assert.match(listed, /Open proposals \(3\):/);
  assert.match(listed, /- p-2 create_project: gamma "Gamma" \(1 work item\(s\)\)\n {2}goal: try gamma\n {2}reason: the owner asked/);
  assert.doesNotMatch(listed, /p-1 |p-5 /, 'superseded and withdrawn proposals are not open');
  assert.match((await commands.run('approve', ' p-1')).text, /not_open/);
  assert.match((await commands.run('approve', ' p-99')).text, /unknown_proposal/);
  assert.match((await commands.run('decline', ' __proto__')).text, /unknown_proposal/);
  const approved = await commands.run('approve', ' p-2');
  assert.equal(approved.kind, 'success', approved.text);
  assert.match(approved.text, /created project gamma at .*projects\/gamma with 1 work item/);
  assert.deepEqual(JSON.parse(await readFile(join(projectsDir, 'gamma', 'work.json'), 'utf8')).items,
    [{ id: 'g1', title: 'write the README', state: 'working', acceptance: seed[0].acceptance }]);
  const meta = JSON.parse(await readFile(join(projectsDir, 'gamma', 'project.json'), 'utf8'));
  assert.deepEqual([meta.project_id, meta.title, meta.goal, meta.proposal_id], ['gamma', 'Gamma', 'try gamma', 'p-2']);
  assert.match(await readFile(join(projectsDir, 'gamma', 'notes.md'), 'utf8'), /try gamma/);
  assert.match((await commands.run('approve', ' p-2')).text, /not_open/, 'an approval runs once');
  // A taken id fails and writes nothing into the existing project.
  const taken = await commands.run('approve', ' p-3');
  assert.equal(taken.kind, 'error'); assert.match(taken.text, /project_id_taken/);
  assert.equal(JSON.parse(await readFile(join(projectsDir, 'alpha', 'project.json'), 'utf8')).title, 'Alpha');
  assert.equal((await commands.run('decline', ' p-4 not now')).kind, 'success');
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.deepEqual(Object.fromEntries(Object.values(stored).map(p => [p.id, p.status])),
    { 'p-1': 'superseded', 'p-2': 'done', 'p-3': 'failed', 'p-4': 'declined', 'p-5': 'withdrawn' });
  for (const id of ['p-2', 'p-3', 'p-4']) assert.equal(stored[id].decided_by, 'owner:local', id);
  assert.equal(stored['p-4'].decline_reason, 'not now');
  // Each decision wakes the steward with its result.
  await until('the decisions never woke the steward', () => api.notified.length === 3);
  const woke = api.notified.map(text => JSON.parse(text.slice(text.indexOf('\n') + 1)));
  assert.deepEqual(woke.map(w => [w.proposal_id, w.status]), [['p-2', 'done'], ['p-3', 'failed'], ['p-4', 'declined']]);
  assert.equal(woke[0].result.project_id, 'gamma');
  assert.equal(woke[2].reason, 'not now');
  // The new project is in the portfolio at once; no half-built directory is left.
  assert.deepEqual((await host.portfolio.projects()).projects.map(p => p.project_id), ['alpha', 'beta', 'gamma']);
  assert.deepEqual((await readdir(projectsDir)).filter(n => n.includes('creating')), []);
  assert.equal((await commands.run('proposals')).text, 'No open proposals.');
  events.stop(); await events.done;
});

test('local host, step 15: a proposal a crash left executing is settled at the next start - done when its project is there, else failed', async () => {
  const { root, projectsDir } = await projectsFixture();
  const payload = id => ({ project_id: id, title: id, goal: 'g', seed_nodes: [] });
  await writeFile(join(root, 'proposals.json'), JSON.stringify({
    'p-1': { id: 'p-1', kind: 'create_project', payload: payload('made'), reason: 'r', status: 'executing', decided_by: 'owner:local' },
    'p-2': { id: 'p-2', kind: 'create_project', payload: payload('lost'), reason: 'r', status: 'executing', decided_by: 'owner:local' } }));
  await mkdir(join(projectsDir, 'made'));
  await writeFile(join(projectsDir, 'made', 'project.json'), JSON.stringify({ proposal_id: 'p-1' }));
  await mkdir(join(projectsDir, '.lost.creating-p-2'));
  const api = fakeApi();
  const events = await localHost({ root, ...required, projectsDir, pollMs: 10 }).events(api);
  await until('the settled proposals never woke the steward', () => api.notified.length === 2);
  events.stop(); await events.done;
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.equal(stored['p-1'].status, 'done');
  assert.equal(stored['p-2'].status, 'failed');
  assert.match(stored['p-2'].result.reason, /interrupted/);
  assert.deepEqual((await readdir(projectsDir)).filter(n => n.includes('creating')), [], 'the half-built directory is removed');
});

test('local host, step 15: a decision is announced before it is final - a wake that could not be written is delivered at the next start', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, ...required, projectsDir, pollMs: 60000 });
  const payload = id => ({ project_id: id, title: id, goal: 'g', seed_nodes: [] });
  for (const id of ['gamma', 'delta', 'eps']) await host.proposals.propose({ kind: 'create_project', payload: payload(id), reason: 'r' });
  // The wake files cannot be written (a directory where each one's tmp file goes).
  await mkdir(join(root, 'inbox', 'proposal-p-1-done.json.tmp'), { recursive: true });
  await mkdir(join(root, 'inbox', 'proposal-p-2-declined.json.tmp'), { recursive: true });
  const commands = fakeCommands();
  const events = await host.events(commandApi(commands));
  await assert.rejects(commands.run('approve', ' p-1'));
  await assert.rejects(commands.run('decline', ' p-2 no'));
  events.stop(); await events.done;
  const before = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.deepEqual([before['p-1'].status, before['p-2'].status], ['executing', 'open'], 'no decision is final before its wake file');
  await rm(join(root, 'inbox', 'proposal-p-1-done.json.tmp'), { recursive: true });
  await rm(join(root, 'inbox', 'proposal-p-2-declined.json.tmp'), { recursive: true });
  // The next start settles the approval from its project and announces it; the decline is given again.
  const again = fakeCommands();
  const api = commandApi(again);
  const restarted = await localHost({ root, ...required, projectsDir, pollMs: 60000 }).events(api);
  assert.equal(api.notified.length, 1, 'the next start announced the approval');
  const woke = JSON.parse(api.notified[0].slice(api.notified[0].indexOf('\n') + 1));
  assert.deepEqual([woke.proposal_id, woke.status, woke.result.project_id], ['p-1', 'done', 'gamma']);
  assert.equal((await again.run('decline', ' p-2 no')).kind, 'success');
  // A project that cannot be put in place (a dangling link at its name) fails and leaves nothing half-built.
  await symlink(join(root, 'nowhere'), join(projectsDir, 'eps'));
  const failed = await again.run('approve', ' p-3');
  assert.equal(failed.kind, 'error'); assert.match(failed.text, /create_failed/);
  assert.deepEqual((await readdir(projectsDir)).filter(n => n.includes('creating')), []);
  restarted.stop(); await restarted.done;
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.deepEqual([stored['p-1'].status, stored['p-2'].status, stored['p-3'].status], ['done', 'declined', 'failed']);
});

test('local host, step 15: /profile shows the profile and its file; /purge asks for the confirmation word, then purges', async () => {
  const root = await freshRoot();
  const memRoot = join(root, 'memory');
  const store = await MemoryStore.open({ root: memRoot, userId: 'owner' });
  try {
    await store.recordTurn('t1', [{ role: 'owner', text: 'my account is 4242' }, { role: 'steward', text: 'noted' }]);
    const commands = fakeCommands();
    const events = await localHost({ root, ...required, pollMs: 10 }).events(commandApi(commands, store));
    assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'profile', 'purge', 'questions']);
    // The owner edits the file directly; /profile reads (and commits) it as the owner's.
    await writeFile(join(memRoot, 'owner', 'profile.md'), 'Likes short replies.');
    const profile = await commands.run('profile');
    assert.equal(profile.kind, 'success');
    assert.match(profile.text, /^Profile \(version [0-9a-f]{40}; 20 of 4000 characters\)\./);
    assert.ok(profile.text.includes(join(memRoot, 'owner', 'profile.md')), profile.text);
    assert.match(profile.text, /Likes short replies\.$/);
    // /purge without the word purges nothing and says how to confirm; a wrong word neither.
    const ask = await commands.run('purge', ' t-t1-0');
    assert.equal(ask.kind, 'success');
    assert.match(ask.text, new RegExp(`^Nothing purged yet\\..*cannot be undone.*/purge t-t1-0 ${PURGE_WORD}$`));
    assert.equal((await commands.run('purge', ' t-t1-0 yes')).kind, 'error');
    assert.deepEqual((await store.read(['t-t1-0'])).not_found ?? [], [], 'nothing was purged before the word');
    assert.equal((await commands.run('purge', '')).kind, 'error');
    const done = await commands.run('purge', ` t-t1-0 ${PURGE_WORD}`);
    assert.equal(done.kind, 'success', done.text);
    assert.match(done.text, /"purged":true/);
    assert.deepEqual((await store.read(['t-t1-0'])).not_found, ['t-t1-0']);
    assert.match((await commands.run('purge', ` t-nope ${PURGE_WORD}`)).text, /unknown_ids/);
    events.stop(); await events.done;
  } finally { await store.close(); }
});
