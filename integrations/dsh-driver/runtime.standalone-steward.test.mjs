/** Plugin step 15 (design-steward 7): the steward on a standalone DSH - a real pinned DSH runs
 *  dsh-director and its built-in local host only (no adapter, no backend), with the owner's
 *  memory and a projectsDir, over two boots of one session. The owner works through DSH's own
 *  command registry (ctx.commands), driven here by a probe plugin that executes a command line
 *  against the steward's live agent exactly as DSH's client does:
 *    boot 1: the steward asks a question and proposes a project; /questions lists it, /answer
 *    answers it (answered_by owner:local) and wakes the steward; /proposals lists the proposal,
 *    /approve creates the project and wakes the steward, which then sees it in its portfolio;
 *    /profile shows the profile. Commands never reach the model.
 *    boot 2: the commands are registered again; nothing is open any more; the new project is
 *    read; /purge asks for its word, then purges a memory entry. The log loads both times. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, rename } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.S15_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-s15-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.S15_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const done = new Set(), boot = process.env.S15_TEST_BOOT;
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  const seed = [{ key: 'g1', goal: 'write the README', acceptance: [{ id: 'c1', kind: 'review', description: 'it reads well' }] }];
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null,
        tools: (options.tools ?? []).map(t => t.name), text: JSON.stringify({ messages: options.messages, tools: options.tools }) });
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: nothing in flight.' };
      else if (!options.tools?.length || options.sessionId !== 's15-root') block = { type: 'text', text: 'Fixture session' };
      else if (boot === '1') block = once('ask', () => tool('ask', 'ask_owner', { question_id: 'q-1', question: 'Which name for the new project?' }))
        ?? once('prop', () => tool('prop', 'propose', { kind: 'create_project', reason: 'the owner asked for a place to try gamma',
          payload: { project_id: 'gamma', title: 'Gamma', goal: 'try gamma', seed_nodes: seed } }))
        ?? once('d1', () => tool('d1', 'idle_dormant', { reason: 'waiting on the owner', wake_after_minutes: 60 }))
        ?? (text.includes('Owner answered') ? once('got', () => ({ type: 'text', text: 'GOT_ANSWER' })) : undefined)
        ?? (text.includes('proposal-p-1-done') ? once('pp', () => tool('pp', 'portfolio_projects', {})) : undefined)
        ?? (done.has('pp') ? once('d1b', () => tool('d1b', 'idle_dormant', { reason: 'nothing left', wake_after_minutes: 60 })) : undefined)
        ?? { type: 'text', text: 'Steward idle.' };
      else block = (text.includes('wake2.json') ? once('pc', () => tool('pc', 'portfolio_project', { project_id: 'gamma' })) : undefined)
        ?? (done.has('pc') ? once('d2', () => tool('d2', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 60 })) : undefined)
        ?? { type: 'text', text: 'Steward idle.' };
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (session.id === 's15-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', event);
  });
}
`;

// What DSH's client does with a slash line: ctx.commands.execute(agent, line, [], signal). Lines
// come in as <dir>/<n>.cmd; the answer goes to <dir>/<n>.out (unresolved: no such command yet).
const PROBE = String.raw`
import { readdir, readFile, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
export const name = 's15-command-probe';
export const inject = ['commands', 'agents'];
export function apply(ctx, { dir, session }) {
  let busy = false;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      for (const file of (await readdir(dir)).filter(n => n.endsWith('.cmd')).sort()) {
        const agent = ctx.agents.get(session);
        if (!agent) break;
        const line = await readFile(join(dir, file), 'utf8');
        let out;
        try { out = (await ctx.commands.execute(agent, line, [], AbortSignal.timeout(60000)))?.result ?? { unresolved: true }; }
        catch (error) { out = { thrown: String(error?.message ?? error) }; }
        await writeFile(join(dir, file.replace(/\.cmd$/, '.out.tmp')), JSON.stringify(out));
        await rename(join(dir, file.replace(/\.cmd$/, '.out.tmp')), join(dir, file.replace(/\.cmd$/, '.out')));
        await rename(join(dir, file), join(dir, file + '.done'));
      }
    } catch {} finally { busy = false; }
  }, 50);
  ctx.on('dispose', () => clearInterval(timer));
}
`;

test('real pinned DSH, two boots: a standalone steward - the owner answers, approves, reads the profile and purges with DSH commands', { timeout: 240000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-s15-'));
  const root = join(directory, 'steward'), memRoot = join(directory, 'memory'), projectsDir = join(directory, 'projects'), cmdDir = join(directory, 'cmds');
  const events = [], requests = [];
  let output = '', proc, n = 0;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const resultText = id => JSON.stringify(result(id).data.message);
  const said = text => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!(await met()) && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(await met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? ''])) + '\n' + output.slice(-3000));
  };
  /** One slash line through DSH's command registry, retried while it is not registered yet. */
  const command = async line => {
    const deadline = Date.now() + 60000;
    for (;;) {
      const name = String(++n).padStart(4, '0');
      await writeFile(join(cmdDir, name + '.tmp'), line);
      await rename(join(cmdDir, name + '.tmp'), join(cmdDir, name + '.cmd'));
      let out;
      await until(`no answer to ${line}`, async () => (out = await readFile(join(cmdDir, name + '.out'), 'utf8').catch(() => undefined)) !== undefined);
      const value = JSON.parse(out);
      if (!value.unresolved || Date.now() > deadline) return value;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  };
  const expected = ['ask_owner', 'close_question', 'grep_chat_history', 'idle_dormant', 'memory_forget', 'memory_note', 'memory_profile_update',
    'memory_read', 'memory_search', 'portfolio_conversation', 'portfolio_project', 'portfolio_projects', 'propose', 'withdraw_proposal'];
  const rootRequests = () => requests.filter(r => r.session === 's15-root' && r.purpose !== 'compaction' && r.tools.length);
  try {
    const seeded = await MemoryStore.open({ root: memRoot, userId: 'owner' });
    await seeded.recordTurn('seed', [{ role: 'owner', part: 'o', text: 'PURGE_ME_4242 my account number' }]);
    await seeded.close();
    await mkdir(join(projectsDir, 'alpha'), { recursive: true });
    await writeFile(join(projectsDir, 'alpha', 'project.json'), JSON.stringify({ title: 'Alpha', goal: 'ship alpha' }));
    await mkdir(join(root, 'inbox'), { recursive: true });
    await mkdir(cmdDir, { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-s15-provider.mjs'), PROVIDER);
    await writeFile(join(directory, 's15-command-probe.mjs'), PROBE);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-s15-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 's15-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          dormantCompactMinTokens: 0, rootRole: 'steward', memory: { userId: 'owner', root: memRoot }, projectsDir } },
        { id: 's15-probe', name: join(directory, 's15-command-probe.mjs'), config: { dir: cmdDir, session: 's15-root' } }] },
    ]));
    const environment = Object.fromEntries(Object.entries(withoutCallerMode(process.env)).filter(([k]) => !k.startsWith('AITELIER_')));
    const launch = boot => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
        DSH_HOME: join(directory, 'home'), S15_TEST_DSH_ENTRY: entry, S15_TEST_BOOT: boot, S15_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };
    const checkTools = (boot, from) => {
      const mine = rootRequests().slice(from);
      assert.ok(mine.length, `boot ${boot}: no steward request`);
      for (const r of mine) assert.deepEqual([...r.tools].sort(), expected, `boot ${boot}: the steward's tool list`);
    };

    // ---- boot 1
    proc = launch('1');
    await until('boot 1 never went dormant', () => result('d1'));
    assert.match(resultText('ask'), /\\"asked\\":true/);
    assert.match(resultText('prop'), /\\"proposal_id\\":\\"p-1\\"/);
    const questions = await command('/questions');
    assert.equal(questions.kind, 'success', JSON.stringify(questions));
    assert.match(questions.text, /- q-1: Which name for the new project\?/);
    const answered = await command('/answer q-1 Call it Gamma');
    assert.equal(answered.kind, 'success', JSON.stringify(answered));
    assert.match(answered.text, /as owner:local/);
    await until('the answer never woke the steward', () => said('GOT_ANSWER'));
    const wake = events.find(e => e.type === 'user/message' && JSON.stringify(e.data).includes('Owner answered'));
    assert.match(JSON.stringify(wake.data), /Call it Gamma/);
    assert.match(JSON.stringify(wake.data), /\\"answered_by\\":\\"owner:local\\"/);
    assert.equal(JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['q-1'].answered_by, 'owner:local');
    const listed = await command('/proposals');
    assert.match(listed.text, /- p-1 create_project: gamma "Gamma"/);
    const approved = await command('/approve p-1');
    assert.equal(approved.kind, 'success', JSON.stringify(approved));
    assert.deepEqual(JSON.parse(await readFile(join(projectsDir, 'gamma', 'work.json'), 'utf8')).items.map(i => i.id), ['g1']);
    await until('the approval never woke the steward', () => result('d1b'));
    assert.match(resultText('pp'), /gamma/);
    assert.match(resultText('pp'), /alpha/);
    const shown = await command('/profile');
    assert.equal(shown.kind, 'success', JSON.stringify(shown));
    assert.match(shown.text, /^Profile \(version /);
    // A command is the owner's: neither the line nor its answer is part of any model request.
    assert.ok(!requests.some(r => r.text.includes('/approve p-1') || r.text.includes('Open proposals')), 'a command reached the model');
    checkTools(1, 0);
    const before = rootRequests().length;
    proc.kill('SIGTERM'); await once(proc, 'exit');

    // ---- boot 2, same session: the commands are registered again
    proc = launch('2');
    assert.equal((await command('/questions')).text, 'No open questions.');
    assert.equal((await command('/proposals')).text, 'No open proposals.');
    await writeFile(join(root, 'inbox', 'wake2.json'), JSON.stringify({ wake: 2 }));
    await until('boot 2 never went dormant', () => result('d2'));
    assert.match(resultText('pc'), /write the README/);
    const ask = await command('/purge t-seed-o');
    assert.match(ask.text, /^Nothing purged yet\./);
    const purged = await command('/purge t-seed-o PURGE');
    assert.equal(purged.kind, 'success', JSON.stringify(purged));
    assert.match(purged.text, /"purged":true/);
    checkTools(2, before);
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt|already registered/);
    proc.kill('SIGTERM'); await once(proc, 'exit');
    const store = await MemoryStore.open({ root: memRoot, userId: 'owner' });
    try { assert.deepEqual((await store.read(['t-seed-o'])).not_found, ['t-seed-o'], 'the purged entry is gone'); }
    finally { await store.close(); }
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
