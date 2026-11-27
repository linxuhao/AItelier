/** The built-in local host: the full conformance suite (it is the serverless CI
 *  gate, design §9 MF5), plus its own two tools and its once-only inbox. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { conformance } from '../conformance/index.mjs';
import { localHost, LOCAL_OWNER, LOCAL_SESSION_ENV, HOST_NOTE, TESTED_DSH_VERSION, prerequisiteProblems, dshVersion,
  apply as applyLocal } from '../hosts/local.mjs';
import { TodoCli, DEFAULT_SESSION_ENV, processSession, backgroundProcesses } from '../lib/core.mjs';
import { startupFailure } from '../lib/exit-cause.mjs';
import { PURGE_WORD, PURGE_CONFIRM_MS, registerOwnerCommands } from '../hosts/local-commands.mjs';
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

test('local host: a steward root is woken by a "Steward event", a director root by a "Director event"', async () => {
  for (const [rootRole, word] of [['steward', 'Steward'], ['director', 'Director'], [undefined, 'Director']]) {
    const root = await freshRoot();
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'a.json'), '{"hello":1}');
    const api = fakeApi();
    const events = await localHost({ root, ...required, pollMs: 20, ...(rootRole ? { rootRole } : {}) }).events(api);
    events.stop(); await events.done;
    assert.deepEqual(api.notified, [`${word} event (a.json):\n{"hello":1}`], String(rootRole));
  }
});

test('local host: work_update and note_write record what the director decides, and the snapshot shows it', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required });
  const tools = new Map();
  const scope = { tools: { register: tool => tools.set(tool.name, tool) }, systemPrompt: { section: () => {} } };
  await host.setup(scope, {}, { defineTool: tool => tool, jsonOutput: {}, runtimeSection: () => {} });
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
  assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'questions', 'status']);
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
  assert.deepEqual(commands.removed.sort(), ['answer', 'questions', 'status'], 'stop() unregisters the commands');
  assert.equal(api.notified.length, 2);
});

test('local host, step 15: /answer writes the wake file first - when questions.json cannot be written, the next start records and delivers it', async () => {
  const root = await freshRoot();
  const host = localHost({ root, ...required, pollMs: 60000 });
  await host.attention.ask({ id: 'q-1', text: 'Restart now?' });
  await host.attention.ask({ id: 'q-2', text: 'Which colour?' });
  const commands = fakeCommands();
  const events = await host.events(commandApi(commands));
  // An answer the owner dropped, still waiting in the inbox, is not answered over.
  await writeFile(join(root, 'inbox', 'answer-q-2.json'), JSON.stringify({ answer: 'blue' }));
  assert.match((await commands.run('answer', ' q-2 red')).text, /answer_waiting/);
  // questions.json cannot be written (a directory where its tmp file goes): /answer fails after the answer file.
  await mkdir(join(root, 'questions.json.tmp'));
  await assert.rejects(commands.run('answer', ' q-1 yes'));
  events.stop(); await events.done;
  assert.deepEqual(JSON.parse(await readFile(join(root, 'inbox', 'answer-q-1.json'), 'utf8')), { answer: 'yes', answered_by: LOCAL_OWNER });
  const before = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'));
  assert.deepEqual([before['q-1'].status, before['q-2'].status], ['open', 'open']);
  await rm(join(root, 'questions.json.tmp'), { recursive: true });
  const api = commandApi(fakeCommands());
  const restarted = await localHost({ root, ...required, pollMs: 60000 }).events(api);
  assert.equal(api.notified.length, 2, 'the next start delivered both answers');
  const stored = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'));
  assert.deepEqual([stored['q-1'].status, stored['q-1'].answer, stored['q-1'].answered_by], ['answered', 'yes', 'owner:local']);
  assert.deepEqual([stored['q-2'].status, stored['q-2'].answer, stored['q-2'].answered_by], ['answered', 'blue', 'owner:local']);
  restarted.stop(); await restarted.done;
});

test('local host, step 15: without a command registry nothing is registered and nothing breaks', async () => {
  const root = await freshRoot();
  const api = fakeApi();
  const events = await localHost({ root, ...required, projectsDir: join(root, 'projects'), pollMs: 10 }).events(api);
  events.stop(); await events.done;
  assert.deepEqual(localHost({ root }).capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'attention'],
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
  assert.deepEqual(host.capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'attention', 'portfolio', 'proposals']);
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

test('local host, step 15: one project whose files do not parse is reported as such and hides no other', async () => {
  const { root, projectsDir } = await projectsFixture();
  await mkdir(join(projectsDir, 'broken'));
  await writeFile(join(projectsDir, 'broken', 'project.json'), '{ edited by hand');
  const listed = (await localHost({ root, projectsDir }).portfolio.projects()).projects;
  assert.deepEqual(listed.map(p => [p.project_id, p.error ?? null]), [['alpha', null], ['beta', null], ['broken', 'unreadable']]);
});

test('local host, step 15: the steward\'s snapshot tells it how its owner answers and decides in this DSH', async () => {
  const { root, projectsDir } = await projectsFixture();
  const plain = JSON.parse(await localHost({ root }).context('steward')).owner_commands;
  assert.match(plain, /\/questions .*\/answer <id> <text>/);
  assert.doesNotMatch(plain, /approve/, 'no projectsDir: no proposals to decide');
  assert.doesNotMatch(plain, /\/forget|\/restore/, 'no memory: no memory commands');
  const full = JSON.parse(await localHost({ root, projectsDir }).context('steward')).owner_commands;
  assert.match(full, /\/proposals .*\/approve <id> or \/decline <id> \[reason\]/);
  assert.doesNotMatch(full, /\/forget/);
  // With memory: memory_forget refuses (no_owner_channel), so the steward is told to give its owner /forget.
  const remembering = JSON.parse(await localHost({ root, projectsDir, memory: { userId: 'owner' } }).context('steward')).owner_commands;
  assert.match(remembering, /\/profile, forget one memory entry with \/forget <id> \[reason\] and bring it back with \/restore <id>, or remove one from every version, for good, with \/purge <id>\. When memory_forget refuses with no_owner_channel, give them \/forget <id> \[reason\] instead\.$/);
  // Every owner command a DSH with proposals and memory registers is named.
  const commands = fakeCommands();
  registerOwnerCommands(commands, { questions: async () => ({}), answer: async () => ({}), proposals: async () => ({}), decide: async () => ({}), memory: {} });
  assert.equal(commands.defs.size, 9);
  for (const name of [...commands.defs.keys()]) assert.ok(remembering.includes('/' + name + ' ') || remembering.includes('/' + name + ','), `owner_commands names /${name}`);
  assert.ok(remembering.startsWith(full.slice(0, -1)), 'the memory commands are added after the others');
  assert.equal(JSON.parse(await localHost({ root, memory: { userId: 'owner' } }).context('director')).owner_commands, undefined);
  assert.equal(JSON.parse(await localHost({ root, projectsDir }).context('director')).owner_commands, undefined, 'the director\'s snapshot is unchanged');
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
  assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'approve', 'decline', 'proposals', 'questions', 'status']);
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
  // A director started on the new project reads its inbox at once: the directory is there.
  assert.deepEqual(await readdir(join(projectsDir, 'gamma', 'inbox')), []);
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
    assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'forget', 'profile', 'purge', 'questions', 'restore', 'status']);
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
    // The word counts only after a warning the owner was shown, and only for a while.
    const unwarned = await commands.run('purge', ` t-t1-1 ${PURGE_WORD}`);
    assert.match(unwarned.text, /^Nothing purged yet\./, 'the word typed at once only warns');
    const now = Date.now;
    try { Date.now = () => now() + PURGE_CONFIRM_MS + 1; assert.match((await commands.run('purge', ` t-t1-1 ${PURGE_WORD}`)).text, /^Nothing purged yet\./, 'an old warning does not count'); }
    finally { Date.now = now; }
    assert.deepEqual((await store.read(['t-t1-1'])).not_found ?? [], [], 'nothing was purged without a standing warning');
    const done = await commands.run('purge', ` t-t1-0 ${PURGE_WORD}`);
    assert.equal(done.kind, 'success', done.text);
    assert.match(done.text, /"purged":true/);
    assert.deepEqual((await store.read(['t-t1-0'])).not_found, ['t-t1-0']);
    // A purged id: /forget says purged, without the /restore hint that /restore would refuse.
    const forgetPurged = await commands.run('forget', ' t-t1-0');
    assert.equal(forgetPurged.kind, 'error');
    assert.equal(forgetPurged.text, 'Not forgotten t-t1-0: purged - it was removed for good and cannot be restored');
    assert.match((await commands.run('restore', ' t-t1-0')).text, /^Not restored t-t1-0: purged/);
    await commands.run('purge', ' t-nope');
    assert.match((await commands.run('purge', ` t-nope ${PURGE_WORD}`)).text, /unknown_ids/);
    events.stop(); await events.done;
  } finally { await store.close(); }
});

test('local host, director 471: /forget and /restore - recoverable forgetting by owner:local, every refusal said, one wake line per action', async () => {
  const root = await freshRoot();
  const memRoot = join(root, 'memory');
  const store = await MemoryStore.open({ root: memRoot, userId: 'owner' });
  try {
    await store.recordTurn('t1', [{ role: 'owner', text: 'my account is 4242' }, { role: 'steward', text: 'noted' }]);
    const commands = fakeCommands();
    const api = commandApi(commands, store);
    const events = await localHost({ root, ...required, pollMs: 10 }).events(api);
    const wakes = () => api.notified.filter(text => /^Director event \(owner-memory-\d+-\d+-[0-9a-f]{8}\.txt\):\n/.test(text));
    // Refusals: no id, an unknown id, an entry that is not forgotten; none changes anything or wakes.
    for (const [name, input, said] of [['forget', '', /^Usage: \/forget <id> \[reason\]$/], ['restore', '  ', /^Usage: \/restore <id>$/],
      ['forget', ' t-nope because', /^Not forgotten t-nope: unknown_id/], ['restore', ' t-t1-0', /^Not restored t-t1-0: not_forgotten/],
      ['restore', ' t-nope', /^Not restored t-nope: not_forgotten/]]) {
      const out = await commands.run(name, input);
      assert.equal(out.kind, 'error', `${name}${input}`); assert.match(out.text, said);
    }
    assert.deepEqual(store.forgottenList(), []);
    // /forget with a reason: gone from reads, on the forgotten list as owner:local, and one line wakes the steward.
    const forgot = await commands.run('forget', ' t-t1-0 it is\nprivate');
    assert.equal(forgot.kind, 'success', forgot.text);
    assert.match(forgot.text, /^Forgotten t-t1-0: .*\/restore t-t1-0 brings it back, \/purge t-t1-0 removes it for good/);
    const gone = await store.read(['t-t1-0']);
    assert.deepEqual([gone.entries.length, gone.forgotten], [0, ['t-t1-0']]);
    assert.deepEqual(store.forgottenList().map(r => [r.id, r.by]), [['t-t1-0', LOCAL_OWNER]]);
    await until('the forget never woke the steward', () => wakes().length === 1);
    assert.equal(wakes()[0].split('\n').filter(Boolean).length, 2, 'one line after the event header');
    assert.match(wakes()[0], /\nThe owner forgot memory entry t-t1-0 with \/forget \(reason: it is private\); it is out of search and reads until they \/restore it\.\n$/);
    // Forgetting it again is refused; without a reason it still forgets.
    const again = await commands.run('forget', ' t-t1-0');
    assert.equal(again.kind, 'error'); assert.match(again.text, /^Not forgotten t-t1-0: already_forgotten - \/restore t-t1-0 brings it back$/);
    assert.equal((await commands.run('forget', ' t-t1-1')).kind, 'success');
    await until('the second forget never woke the steward', () => wakes().length === 2);
    assert.match(wakes()[1], /reason: no reason given/);
    // /restore: back in reads, off the forgotten list, one wake; restoring it twice is refused.
    const restored = await commands.run('restore', ' t-t1-0');
    assert.equal(restored.kind, 'success', restored.text);
    assert.match(restored.text, /^Restored t-t1-0: it is back in search and reads\.$/);
    assert.deepEqual((await store.read(['t-t1-0'])).entries.map(e => e.id), ['t-t1-0']);
    assert.deepEqual(store.forgottenList().map(r => r.id), ['t-t1-1']);
    await until('the restore never woke the steward', () => wakes().length === 3);
    assert.match(wakes()[2], /\nThe owner restored memory entry t-t1-0 with \/restore; it is back in search and reads\.\n$/);
    const twice = await commands.run('restore', ' t-t1-0');
    assert.equal(twice.kind, 'error'); assert.match(twice.text, /^Not restored t-t1-0: not_forgotten/);
    // Who did it is in the memory's own history.
    const { execFileSync } = await import('node:child_process');
    const log = execFileSync('git', ['-C', store.dir, 'log', '--format=%s']).toString();
    assert.match(log, /restore t-t1-0 \(owner:local\)/); assert.match(log, /forget t-t1-0 \(owner:local\)/);
    events.stop(); await events.done;
    assert.equal(wakes().length, 3, 'a refusal never wakes the steward');
  } finally { await store.close(); }
});

test('local host, director 471: a wake that cannot be written is said, and the forget still stands', async () => {
  const commands = fakeCommands();
  const calls = [];
  const memory = { forget: async (ids, o) => (calls.push(['forget', ids, o]), { forgotten: ids, already: [], unknown: [], remote_forget: 'local', note: 'n' }),
    restore: async (id, by) => (calls.push(['restore', id, by]), { restored: id }) };
  registerOwnerCommands(commands, { questions: async () => ({}), answer: async () => ({}), memory, wake: async () => { throw new Error('disk full'); } });
  const forgot = await commands.run('forget', ' m-1 old');
  assert.equal(forgot.kind, 'success'); assert.match(forgot.text, /Your steward was not told: disk full\.$/);
  const restored = await commands.run('restore', ' m-1');
  assert.equal(restored.kind, 'success'); assert.match(restored.text, /Your steward was not told: disk full\.$/);
  assert.deepEqual(calls, [['forget', ['m-1'], { reason: 'old', by: 'owner:local' }], ['restore', 'm-1', 'owner:local']]);
});

test('local host, director 472: a wake written after a restart in the same millisecond does not overwrite an undelivered one', async () => {
  const root = await freshRoot();
  const memory = { forget: async ids => ({ forgotten: ids, already: [], unknown: [], remote_forget: 'local', note: 'n' }), restore: async id => ({ restored: id }) };
  const now = Date.now;
  try {
    Date.now = () => 1700000000000;
    // Two process lives, each with its counter at 0; a disposed api delivers nothing, so the first wake waits.
    for (const [name, input] of [['forget', ' m-1 old'], ['restore', ' m-1']]) {
      const commands = fakeCommands();
      const events = await localHost({ root, ...required, pollMs: 600000 }).events({ ...commandApi(commands, memory), isDisposed: () => true });
      assert.equal((await commands.run(name, input)).kind, 'success');
      events.stop(); await events.done;
    }
  } finally { Date.now = now; }
  const names = (await readdir(join(root, 'inbox'))).filter(name => name.startsWith('owner-memory-')).sort();
  assert.equal(names.length, 2, names.join(' '));
  for (const name of names) assert.match(name, /^owner-memory-1700000000000-1-[0-9a-f]{8}\.txt$/);
  const lines = await Promise.all(names.map(name => readFile(join(root, 'inbox', name), 'utf8')));
  assert.deepEqual(lines.map(line => /forgot|restored/.exec(line)[0]).sort(), ['forgot', 'restored']);
});

// ---- Review 467 (s15 section 2): bounded portfolio pages, tolerant proposal records,
// replayed and never-reused proposal ids, and the nits.

test('local host, review 467 F1: portfolio.project pages 2000 open work items under 9000 characters, by id cursor, and says when there is no more', async () => {
  const { root, projectsDir } = await projectsFixture();
  const items = Array.from({ length: 2000 }, (_, i) => ({ id: `n${String(i).padStart(4, '0')}`, title: `item ${i} `.repeat(4), state: i % 10 === 0 ? 'completed' : 'working' }));
  await writeFile(join(projectsDir, 'alpha', 'work.json'), JSON.stringify({ items: [...items].reverse() }));
  const host = localHost({ root, projectsDir });
  const seen = [];
  let after, pages = 0;
  do {
    const page = await host.portfolio.project('alpha', undefined, { openAfter: after });
    pages++;
    assert.ok(JSON.stringify(page).length <= 9000, `page ${pages} is ${JSON.stringify(page).length} characters`);
    assert.ok(page.open_work.length >= 1);
    assert.equal(page.nodes_shown.open_total, 1800);
    assert.equal(page.nodes_shown.open, page.open_work.length);
    assert.equal(page.nodes_shown.open_after, after ?? null);
    assert.equal(page.nodes_shown.open_remaining_after_page, 1800 - seen.length - page.open_work.length);
    seen.push(...page.open_work.map(item => item.id));
    after = page.nodes_shown.open_next_after;
    if (after !== null) assert.equal(after, seen.at(-1), 'the cursor is the last id shown');
  } while (after !== null && pages < 1000);
  assert.ok(pages > 10, `only ${pages} page(s)`);
  assert.deepEqual(seen, items.filter(item => item.state === 'working').map(item => item.id), 'every open item once, in id order');
  assert.deepEqual((await host.portfolio.project('alpha')).work_counts, { working: 1800, completed: 200 });
  // An empty list says "no more" too; a bad cursor is refused by name, never read as "start again".
  const empty = await host.portfolio.project('beta');
  assert.deepEqual([empty.open_work, empty.nodes_shown.open_next_after, empty.nodes_shown.open_remaining_after_page], [[], null, 0]);
  assert.equal((await host.portfolio.project('alpha', undefined, { openAfter: 7 })).open_work.refused, 'invalid_open_after');
});

test('local host, review 467 F1: portfolio.conversation pages 500 questions and answers newest first, with a cursor and has_more', async () => {
  const { root, projectsDir } = await projectsFixture();
  const qs = {};
  for (let i = 0; i < 500; i++) {
    const asked = new Date(Date.UTC(2026, 10, 1) + i * 60000).toISOString();
    qs[`q-${i}`] = i % 2 ? { text: `Question ${i}? `.repeat(5), status: 'open', asked_at: asked }
      : { text: `Question ${i}?`, status: 'answered', asked_at: asked, answer: `answer ${i} `.repeat(5), answered_by: 'owner:local',
        answered_at: new Date(Date.UTC(2026, 10, 1) + i * 60000 + 30000).toISOString() };
  }
  await writeFile(join(projectsDir, 'alpha', 'questions.json'), JSON.stringify(qs));
  const host = localHost({ root, projectsDir });
  const seen = [];
  let cursor, pages = 0, page;
  do {
    page = await host.portfolio.conversation('alpha', cursor);
    pages++;
    assert.ok(JSON.stringify(page).length <= 9000, `page ${pages} is ${JSON.stringify(page).length} characters`);
    assert.equal(page.messages_total, 500);
    assert.equal(page.has_more, page.next_cursor !== null);
    assert.equal(page.remaining_after_page, 500 - seen.length - page.messages.length);
    seen.push(...page.messages.map(m => m.id));
    cursor = page.next_cursor;
  } while (cursor !== null && pages < 1000);
  assert.ok(pages > 10, `only ${pages} page(s)`);
  assert.deepEqual(seen, Array.from({ length: 500 }, (_, i) => `q-${499 - i}`), 'every message once, newest first');
  const none = await host.portfolio.conversation('beta');
  assert.deepEqual([none.messages, none.has_more, none.next_cursor], [[], false, null], 'an empty conversation says there is no more');
  assert.equal((await host.portfolio.conversation('alpha', 'not-a-cursor')).refused, 'invalid_cursor');
  // The open questions in the project summary are bounded too: the newest 20, with their total.
  const alpha = await host.portfolio.project('alpha');
  assert.equal(alpha.open_questions.length, 20);
  assert.equal(alpha.open_questions_total, 250);
  assert.equal(alpha.open_questions[0].id, 'q-499');
});

test('local host, review 467 F2: a proposal record edited into another shape is shown for what it is; /approve fails it by name, the others still list', async () => {
  const { root, projectsDir } = await projectsFixture();
  const good = { project_id: 'gamma', title: 'Gamma', goal: 'try gamma', seed_nodes: [] };
  await writeFile(join(root, 'proposals.json'), JSON.stringify({
    'p-1': { id: 'p-1', kind: 'create_project', payload: null, reason: 'hand-edited', status: 'open', created_at: '2026-11-01T00:00:00Z' },
    'p-2': { id: 'p-2', kind: 'create_project', payload: { project_id: 'delta' }, reason: 'half', status: 'open', created_at: '2026-11-02T00:00:00Z' },
    'p-3': null,
    'p-4': { id: 'p-4', kind: 'create_project', payload: good, reason: 'whole', status: 'open', created_at: '2026-11-03T00:00:00Z' } }));
  const commands = fakeCommands();
  const api = commandApi(commands);
  const events = await localHost({ root, projectsDir, pollMs: 10 }).events(api);
  const listed = await commands.run('proposals');
  assert.equal(listed.kind, 'success', listed.text);
  assert.match(listed.text, /Open proposals \(3\):/);
  assert.match(listed.text, /- p-1 create_project: \? "" \(0 work item\(s\)\)[\s\S]*not a whole create_project/);
  assert.match(listed.text, /- p-4 create_project: gamma "Gamma"/);
  for (const id of ['p-1', 'p-2']) {
    const approved = await commands.run('approve', ` ${id}`);
    assert.equal(approved.kind, 'error'); assert.match(approved.text, /invalid_proposal/);
  }
  assert.match((await commands.run('approve', ' p-3')).text, /unknown_proposal/);
  assert.equal((await commands.run('approve', ' p-4')).kind, 'success');
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.deepEqual([stored['p-1'].status, stored['p-2'].status, stored['p-4'].status], ['failed', 'failed', 'done']);
  assert.deepEqual((await readdir(projectsDir)).sort(), ['.hidden', 'alpha', 'beta', 'gamma', 'not-a-dir'], 'nothing half-built');
  await until('the failures never woke the steward', () => api.notified.length === 3);
  events.stop(); await events.done;
});

test('local host, review 467 F3: a retried propose answers with the proposal it made; after a decline the same proposal is a new card', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, projectsDir, pollMs: 60000 });
  const call = { kind: 'create_project', payload: { project_id: 'gamma', title: 'Gamma', goal: 'try gamma' }, reason: 'the owner asked' };
  assert.deepEqual(await host.proposals.propose(call), { proposed: true, proposal_id: 'p-1', status: 'open' });
  assert.deepEqual(await host.proposals.propose(call), { proposed: false, duplicate: true, proposal_id: 'p-1', status: 'open' });
  const changed = { ...call, payload: { ...call.payload, title: 'Gamma 2' }, supersedes: 'p-1' };
  assert.equal((await host.proposals.propose(changed)).proposal_id, 'p-2');
  assert.deepEqual(await host.proposals.propose(changed), { proposed: false, duplicate: true, proposal_id: 'p-2', status: 'open' }, 'a retried supersede is not refused');
  assert.deepEqual(await host.proposals.propose(call), { proposed: false, duplicate: true, proposal_id: 'p-1', status: 'superseded' });
  const commands = fakeCommands();
  const events = await host.events(commandApi(commands));
  await commands.run('decline', ' p-2 not now');
  events.stop(); await events.done;
  assert.equal((await host.proposals.propose(changed)).refused, 'not_open', 'after the decline it is proposed again, and p-1 is no longer open');
  assert.equal((await host.proposals.propose({ ...changed, supersedes: undefined })).proposal_id, 'p-3');
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.equal(Object.values(stored).filter(p => p.status === 'open').length, 1, 'one open card');
  assert.match(stored['p-1'].request_key, /^proposal:[0-9a-f]{40}$/);
});

test('local host, review 467 F3: a proposal id is never given twice, even after proposals are deleted by hand', async () => {
  const { root, projectsDir } = await projectsFixture();
  const host = localHost({ root, projectsDir });
  const call = id => ({ kind: 'create_project', payload: { project_id: id, title: id, goal: 'g' }, reason: 'r' });
  for (const id of ['a1', 'a2', 'a3']) await host.proposals.propose(call(id));
  const file = join(root, 'proposals.json');
  const edit = async drop => { const all = JSON.parse(await readFile(file, 'utf8')); for (const id of drop) delete all[id]; await writeFile(file, JSON.stringify(all)); };
  await edit(['p-1']);
  assert.equal((await host.proposals.propose(call('b1'))).proposal_id, 'p-4', 'deleting an old one does not overwrite p-3');
  await edit(['p-4']);
  assert.equal((await host.proposals.propose(call('b2'))).proposal_id, 'p-5', 'deleting the newest does not give its id again');
  const all = JSON.parse(await readFile(file, 'utf8'));
  assert.deepEqual(Object.keys(all).sort(), ['p-2', 'p-3', 'p-5']);
  assert.equal(all['p-3'].payload.project_id, 'a3');
});

test('local host, review 467: recovery touches no path a hand-edited project id names', async () => {
  const { root, projectsDir } = await projectsFixture();
  await writeFile(join(root, 'proposals.json'), JSON.stringify({
    'p-1': { id: 'p-1', kind: 'create_project', payload: { project_id: '/../victim', title: 't', goal: 'g' }, reason: 'r', status: 'executing' },
    '../p-2': { id: '../p-2', kind: 'create_project', payload: { project_id: 'x', title: 't', goal: 'g' }, reason: 'r', status: 'executing' } }));
  await mkdir(join(root, 'victim.creating-p-1'));
  await mkdir(join(projectsDir, '.x.creating-..', 'p-2'), { recursive: true });
  const api = fakeApi();
  const events = await localHost({ root, projectsDir, pollMs: 60000 }).events(api);
  events.stop(); await events.done;
  assert.ok((await readdir(root)).includes('victim.creating-p-1'), 'a directory outside projectsDir was removed');
  assert.deepEqual(await readdir(join(projectsDir, '.x.creating-..')), ['p-2'], 'a path named by a hand-edited proposal id was removed');
  assert.equal(api.notified.length, 2, 'both are announced, each from a file inside the inbox');
  const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'));
  assert.deepEqual([stored['p-1'].status, stored['../p-2'].status], ['failed', 'failed']);
});

test('local host, review 467: stop() is safe when the registry\'s register returns no disposer', async () => {
  const root = await freshRoot();
  const registered = [];
  const commands = { register: def => { registered.push(def.name); } };
  const events = await localHost({ root, pollMs: 60000 }).events(commandApi(commands));
  assert.deepEqual(registered.sort(), ['answer', 'questions', 'status']);
  events.stop(); await events.done;
});

test('local host, review 467: /profile and /purge tell the owner when the configured memory did not open', async () => {
  const root = await freshRoot();
  const commands = fakeCommands();
  const events = await localHost({ root, pollMs: 60000, memory: { userId: 'owner' } })
    .events({ ...commandApi(commands), memoryError: 'unknown time zone Mars/Base' });
  assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'forget', 'profile', 'purge', 'questions', 'restore', 'status']);
  for (const name of ['profile', 'purge', 'forget', 'restore']) {
    const said = await commands.run(name, ' t-1');
    assert.equal(said.kind, 'error');
    assert.match(said.text, /memory is not available: unknown time zone Mars\/Base/);
  }
  events.stop(); await events.done;
  // No memory configured: no memory commands, as before.
  const plain = fakeCommands();
  const again = await localHost({ root, pollMs: 60000 }).events(commandApi(plain));
  assert.deepEqual([...plain.defs.keys()].sort(), ['answer', 'questions', 'status']);
  again.stop(); await again.done;
});

test('local host: a configured memory that did not open is said on stderr and in progress.jsonl at startup', async () => {
  const root = await freshRoot();
  const printed = [], flags = [];
  const saved = console.error;
  console.error = line => printed.push(line);
  try {
    const events = await localHost({ root, pollMs: 60000, memory: { userId: 'owner' } })
      .events({ ...fakeApi(), memoryError: 'memory: locked by process 4242', setBasicsFlag: async text => { flags.push(text); } });
    events.stop(); await events.done;
    // Memory that opened, or none configured: nothing is said.
    const quiet = await localHost({ root, pollMs: 60000 }).events({ ...fakeApi(), setBasicsFlag: async text => { flags.push(text); } });
    quiet.stop(); await quiet.done;
  } finally { console.error = saved; }
  assert.deepEqual(printed, ['dsh-director: memory unavailable: memory: locked by process 4242']);
  assert.deepEqual(flags, ['memory_unavailable: memory: locked by process 4242']);
});

test('local host, review 467: /questions and /proposals list oldest first by time, not by file order', async () => {
  const { root, projectsDir } = await projectsFixture();
  await writeFile(join(root, 'questions.json'), JSON.stringify({
    'q-b': { text: 'later', status: 'open', asked_at: '2026-11-02T00:00:00Z' },
    'q-a': { text: 'earlier', status: 'open', asked_at: '2026-11-01T00:00:00Z' } }));
  const proposal = (project_id, created_at) => ({ kind: 'create_project', payload: { project_id, title: project_id, goal: 'g', seed_nodes: [] },
    reason: 'r', status: 'open', created_at });
  await writeFile(join(root, 'proposals.json'), JSON.stringify({
    'p-9': proposal('late', '2026-11-05T00:00:00Z'), 'p-10': proposal('early', '2026-11-04T00:00:00Z') }));
  const commands = fakeCommands();
  const events = await localHost({ root, projectsDir, pollMs: 60000 }).events(commandApi(commands));
  const questions = (await commands.run('questions')).text;
  assert.ok(questions.indexOf('q-a') < questions.indexOf('q-b'), questions);
  const proposals = (await commands.run('proposals')).text;
  assert.ok(proposals.indexOf('p-10') < proposals.indexOf('p-9'), proposals);
  events.stop(); await events.done;
});

test('local host, review 467: the owner\'s commands answer only in the root session\'s UI', async () => {
  const root = await freshRoot();
  await localHost({ root }).attention.ask({ id: 'q-1', text: 'Restart now?' });
  const commands = fakeCommands();
  const api = { ...commandApi(commands), agent: { session: { id: 'root-session' } } };
  const events = await localHost({ root, pollMs: 60000 }).events(api);
  const run = (name, input, agent) => commands.defs.get(name).handler({ rawInput: input, agent, attachments: [], signal: AbortSignal.timeout(5000) });
  const elsewhere = await run('answer', ' q-1 yes', { session: { id: 'worker-7' } });
  assert.equal(elsewhere.kind, 'error');
  assert.match(elsewhere.text, /only in your director's own session/);
  assert.equal(JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'].status, 'open', 'nothing was answered from another UI');
  assert.equal((await run('answer', ' q-1 yes', { session: { id: 'root-session' } })).kind, 'success');
  events.stop(); await events.done;
});

test('local host, review 467: steward.md tells a standalone steward to use its owner_commands, and keeps the chat-channel phrase', async () => {
  const md = await readFile(new URL('../steward.md', import.meta.url), 'utf8');
  assert.match(md, /`同意 proposal <id>` to approve, `拒绝 proposal <id>` to decline/);
  assert.match(md, /send that line back from Slack or WeChat as\s+well as decide on the dashboard/);
  assert.match(md, /If your context snapshot has owner_commands, your owner has no Slack, WeChat or\s+dashboard: give them the commands it names instead of that line/);
});

// ---- Overfit review 2026-11-24 (standalone): what a stranger's first start needs.

test('local host config: provider and model are required, the rest has working defaults', () => {
  assert.throws(() => validateHost(localHost({ root: '/tmp/x' })), /missing config: provider; missing config: model/);
  const { config } = localHost({ root: '/tmp/x', provider: 'p', model: 'm', permissionPreset: 'danger-full-access' });
  assert.ok(config.sessionEnv.startsWith(LOCAL_SESSION_ENV + '_'), config.sessionEnv);
  assert.ok(!LOCAL_SESSION_ENV.startsWith('DSH_'), 'DSH drops ambient DSH_* variables from every model shell');
  assert.equal(config.permissionPreset, 'danger-full-access');
  assert.equal(config.startupErrorDetail, true);
  assert.equal(config.planKeysOptional, true);
  assert.equal(localHost({ root: '/tmp/x' }).config.permissionPreset, undefined, 'no preset unless asked for');
});

test('local host session tag: one variable per director root, so two directors on one machine do not see each other\'s jobs', () => {
  // Review r2 (nit): with one fixed variable and the default sessionId every local director's
  // jobs carried DIRECTOR_LOCAL_SESSION=director, so watch_process and idle_dormant of one
  // director counted the other's jobs.
  const env = options => localHost({ provider: 'p', model: 'm', ...options }).config.sessionEnv;
  const a = env({ root: '/tmp/director-a' }), b = env({ root: '/tmp/director-b' });
  assert.notEqual(a, b);
  for (const name of [a, b]) assert.match(name, /^DIRECTOR_LOCAL_SESSION_[0-9a-f]{12}$/);
  assert.equal(env({ root: '/tmp/director-a/../director-a/' }), a, 'the same root, written another way, is the same director');
  assert.equal(env({ root: '/tmp/director-a', sessionEnv: 'MY_TAG' }), 'MY_TAG', 'a configured variable wins');
});

test('core default session tag (a host that names no sessionEnv) is not a DSH_ name', async () => {
  // Union review 2026-11-24 (standalone f2): DSH 0.2.1-rc.5 drops DSH_* from every model shell, so
  // with the old default DSH_DIRECTOR_SESSION the director's own job carried no tag:
  // watch_process refused it (not_this_driver) and its end event never arrived.
  assert.ok(!DEFAULT_SESSION_ENV.startsWith('DSH_'), DEFAULT_SESSION_ENV);
  const proc = await mkdtemp(join(tmpdir(), 'proc-'));
  roots.push(proc);
  await mkdir(join(proc, '4242'));
  await writeFile(join(proc, '4242', 'environ'), `PATH=/bin\0${DEFAULT_SESSION_ENV}=dir-1\0`);
  await writeFile(join(proc, '4242', 'stat'), '4242 (sh) S 1');
  assert.equal(processSession(4242, proc), 'dir-1', 'the default is what processSession reads');
  assert.deepEqual(backgroundProcesses('dir-1', 1, proc), [4242], 'and what idle_dormant counts');
});

test('local host dshEntry: the running entry with its symlinks resolved (a global dsh is a link)', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-entry-'));
  roots.push(dir);
  await mkdir(join(dir, 'lib/node_modules/@deepseek-ai/dsh/lib'), { recursive: true });
  const real = join(dir, 'lib/node_modules/@deepseek-ai/dsh/lib/bin.js');
  await writeFile(real, '');
  await writeFile(join(dir, 'lib/node_modules/@deepseek-ai/dsh/package.json'), JSON.stringify({ name: '@deepseek-ai/dsh', version: '9.9.9' }));
  await mkdir(join(dir, 'bin'));
  await symlink(real, join(dir, 'bin/dsh'));
  const saved = process.argv[1];
  process.argv[1] = join(dir, 'bin/dsh');
  try { assert.equal(localHost({ root: '/tmp/x' }).config.dshEntry, real); }
  finally { process.argv[1] = saved; }
  assert.equal(localHost({ root: '/tmp/x', dshEntry: '/given' }).config.dshEntry, '/given');
  assert.equal(dshVersion(join(dir, 'bin/dsh')), '9.9.9');
  assert.equal(dshVersion(join(dir, 'nowhere.js')), undefined);
});

test('local host roles(): its two recording tools are shown to the director, nothing else changes', async () => {
  const { loadRoles } = await import('../lib/roles.mjs');
  const host = localHost({ root: '/tmp/x' });
  const table = loadRoles(host.config.rolesFile);
  const effective = host.roles(table);
  assert.deepEqual(effective.director.presented, [...table.director.presented, 'work_update', 'note_write']);
  assert.deepEqual({ ...effective, director: table.director }, table);
  assert.deepEqual(table.director.presented.includes('work_update'), false, 'the table itself is not modified');
});

test('local host setup: the director reads the host note after its role prompt; a steward does not', async () => {
  const run = async rootRole => {
    const sections = [], calls = [];
    const scope = { tools: { register: () => {} }, systemPrompt: { section: section => sections.push(section) } };
    await localHost({ root: '/tmp/x', ...(rootRole ? { rootRole } : {}) }).setup(scope, {},
      { defineTool: tool => tool, jsonOutput: {}, runtimeSection: () => calls.push('runtime') });
    return { sections, calls };
  };
  const director = await run();
  assert.deepEqual(director.calls, ['runtime']);
  assert.equal(director.sections.length, 1);
  assert.equal(director.sections[0].text, HOST_NOTE);
  assert.ok(director.sections[0].order > 12, 'after the role prompt (order 12)');
  assert.match(HOST_NOTE, /work_update/); assert.match(HOST_NOTE, /note_write/); assert.match(HOST_NOTE, /inbox\/answer-<id>\.json/);
  const steward = await run('steward');
  assert.deepEqual(steward, { sections: [], calls: [] });
});

test('local host prerequisites: each missing piece is named, and a working machine has none', async () => {
  const cwd = new URL('../todo', import.meta.url).pathname;
  const state = await mkdtemp(join(tmpdir(), 'dsh-prereq-'));
  roots.push(state);
  const real = new TodoCli({ python: 'python3', cwd, stateDir: state, module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' });
  assert.deepEqual(await prerequisiteProblems({ todo: real }), []);
  const noPython = new TodoCli({ python: '/nonexistent/python3', cwd, stateDir: state, module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' });
  const [problem] = await prerequisiteProblems({ todo: noPython });
  assert.match(problem, /^python not found: \/nonexistent\/python3/);
  // The CLI lists a template it cannot load with its error: with no PyYAML that is every template.
  const noYaml = { python: 'python3', run: async () => ({ code: 0, result: { templates: [{ name: 'a', error: "ModuleNotFoundError: No module named 'yaml'" }] } }) };
  // Review r2: Debian and Ubuntu's python3 has no pip, and pip refuses their system Python
  // (PEP 668), so the distribution's package is named first.
  assert.deepEqual(await prerequisiteProblems({ todo: noYaml }), ['PyYAML is missing for python3 (Debian/Ubuntu: apt install python3-yaml; '
    + `otherwise python3 -m pip install -r ${new URL('../requirements.txt', import.meta.url).pathname}, in a venv if pip refuses)`]);
  const readme = await readFile(new URL('../README.md', import.meta.url), 'utf8');
  assert.ok(readme.includes('apt install python3-yaml'), 'README.md names the Debian/Ubuntu package');
  const requirements = await readFile(new URL('../requirements.txt', import.meta.url), 'utf8');
  assert.ok(requirements.includes('apt install python3-yaml'), 'requirements.txt names the Debian/Ubuntu package');
  const noGit = await prerequisiteProblems({ todo: real, git: '/nonexistent/git' });
  assert.deepEqual(noGit, ['git not found (no /nonexistent/git on PATH): install git']);
  assert.match((await prerequisiteProblems({ todo: real, platform: 'win32' }))[0], /^Windows is not supported/);
  // A steward needs neither python nor (without memory) git.
  assert.deepEqual(await prerequisiteProblems({ todo: noPython, director: false, git: '/nonexistent/git' }), []);
  assert.equal((await prerequisiteProblems({ todo: noPython, director: false, memory: true, git: '/nonexistent/git' })).length, 1);
});

test('local host install(): refuses to start naming what is missing; another DSH release only warns', async () => {
  const warned = [];
  const api = (python, dshEntry = '/nowhere/bin.js') => ({ todo: new TodoCli({ python, cwd: new URL('../todo', import.meta.url).pathname,
    stateDir: '/tmp/x/state', module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' }), config: { dshEntry },
  ctx: { logger: { warn: line => warned.push(line) } } });
  await assert.rejects(localHost({ root: '/tmp/x' }).install(api('/nonexistent/python3')),
    /^Error: dsh-director local host cannot start here: python not found/);
  await localHost({ root: '/tmp/x' }).install(api('python3'));
  assert.deepEqual(warned, [`dsh-director local host: tested on DSH ${TESTED_DSH_VERSION}, running an unknown version`]);
});

test('local host progress: a failed phase is printed to stderr once per distinct error', async () => {
  const root = await freshRoot();
  const host = localHost({ root });
  const printed = [];
  const saved = console.error;
  console.error = line => printed.push(line);
  try {
    await host.progress({ phase: 'running', subagents: [], error: '' });
    await host.progress({ phase: 'failed', subagents: [], error: 'Model completion: no API key for deepseek-official\nmore' });
    await host.progress({ phase: 'failed', subagents: [], error: 'Model completion: no API key for deepseek-official\nmore' });
    await host.progress({ phase: 'waiting', subagents: [], error: '' });
    await host.progress({ phase: 'failed', subagents: [], error: 'Model completion: no API key for deepseek-official\nmore' });
  } finally { console.error = saved; }
  assert.deepEqual(printed, ['dsh-director failed: Model completion: no API key for deepseek-official',
    'dsh-director failed: Model completion: no API key for deepseek-official']);
  assert.equal((await readFile(join(root, 'progress.jsonl'), 'utf8')).trim().split('\n').length, 5, 'progress.jsonl keeps every report');
});

test('local host apply(): the session tag is set on this process, and put back on dispose', () => {
  const key = 'DIRECTOR_LOCAL_SESSION_TEST';
  delete process.env[key];
  const handlers = {}, provided = [];
  const ctx = { on: (name, fn) => { handlers[name] = fn; }, provide: (name, value) => provided.push([name, value]) };
  applyLocal(ctx, { root: '/tmp/x', sessionId: 'dir-1', sessionEnv: key });
  assert.equal(process.env[key], 'dir-1');
  assert.equal(provided[0][0], 'directorHost');
  handlers.dispose();
  assert.equal(process.env[key], undefined);
});

test('local host apply() without config: the process ends with the reason, it does not wait for a host that will never come', () => {
  const printed = [], exits = [];
  const saved = console.error;
  console.error = line => printed.push(line);
  const slot = Symbol.for('dsh.exit-cause');
  delete globalThis[slot];
  try {
    const ctx = { on: () => {}, provide: () => assert.fail('provided a host'), logger: { error: () => {} },
      get: name => name === 'appExit' ? code => exits.push(code) : undefined };
    assert.throws(() => applyLocal(ctx, undefined), /missing config: root/);
  } finally { console.error = saved; delete globalThis[slot]; }
  assert.deepEqual(exits, [1]);
  assert.deepEqual(printed, ['dsh exit cause: dsh-director startup: Error',
    'dsh-director startup: directorHost refused: missing config: root (the local host keeps its files there)']);
});

test('startupFailure: with detail the first line of the reason follows the exit line; without, only the exit line', () => {
  const printed = [];
  const saved = console.error;
  console.error = line => printed.push(line);
  try {
    const ctx = () => ({ logger: { error: () => {} }, get: () => undefined });
    const slot = Symbol.for('dsh.exit-cause');
    delete globalThis[slot];
    startupFailure(ctx(), new Error('directorHost refused: missing config: provider\nsecond line'), { detail: true });
    delete globalThis[slot];
    startupFailure(ctx(), new Error('a reason a host prints itself'));
    delete globalThis[slot];
  } finally { console.error = saved; }
  assert.deepEqual(printed, ['dsh exit cause: dsh-director startup: Error', 'dsh-director startup: directorHost refused: missing config: provider',
    'dsh exit cause: dsh-director startup: Error']);
});

test('startupFailure: an error with a code keeps its class on the exit line, and the reason still follows (README.md says so)', async () => {
  const printed = [];
  const saved = console.error;
  console.error = line => printed.push(line);
  try {
    const slot = Symbol.for('dsh.exit-cause');
    delete globalThis[slot];
    const error = Object.assign(new Error("ENOENT: no such file or directory, open '/nonexistent/roles.json'"), { code: 'ENOENT' });
    startupFailure({ logger: { error: () => {} }, get: () => undefined }, error, { detail: true });
    delete globalThis[slot];
  } finally { console.error = saved; }
  assert.deepEqual(printed, ['dsh exit cause: dsh-director startup: connection-level ENOENT',
    "dsh-director startup: ENOENT: no such file or directory, open '/nonexistent/roles.json'"]);
  const readme = await readFile(new URL('../README.md', import.meta.url), 'utf8');
  assert.ok(readme.includes('dsh exit cause: dsh-director startup: connection-level ENOENT'), 'README.md shows the coded exit line too');
});

test('the tested DSH release is the repository\'s pin, and README.md names it', async t => {
  const readme = await readFile(new URL('../README.md', import.meta.url), 'utf8');
  assert.ok(readme.includes(`@deepseek-ai/dsh@${TESTED_DSH_VERSION}`) && readme.includes(`DSH \`${TESTED_DSH_VERSION}\``), 'README.md names another DSH release');
  // The pin lives beside the backend adapter; a copy of this plugin alone has no such file.
  const pin = await readFile(new URL('../../dsh-driver/package.json', import.meta.url), 'utf8').catch(() => undefined);
  if (!pin) return t.skip('no ../dsh-driver/package.json next to this plugin');
  assert.equal(JSON.parse(pin).dependencies['@deepseek-ai/dsh'], TESTED_DSH_VERSION);
});
