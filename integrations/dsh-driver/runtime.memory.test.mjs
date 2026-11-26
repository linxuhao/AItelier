/** Plugin step 11 (design-steward 3.4/3.7): the owner's memory in a real pinned DSH, with
 *  the plugin's built-in local host (no backend) and a fixture model, over two boots.
 *
 *  Boot 1: the startup snapshot shows the empty profile and its version; the model
 *  updates the profile with that version, writes a note, finds it, and goes dormant,
 *  which compacts; the next turn's post-compaction snapshot carries the new profile -
 *  after compaction the profile text can come from nowhere else.
 *  Boot 2 (same session): the note is still found, the profile is updated again from the
 *  version the last snapshot showed, and the next compaction's snapshot carries it.
 *  Throughout, the memory tools are registered but not presented (roles.json decides). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { readFileSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';
import { localSessionEnv } from '../dsh-director/hosts/local.mjs';
import { INTERNAL_ENV, INTERNAL_VALUE } from '../dsh-director/lib/core.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.MEM_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-memory-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.MEM_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const lastVersion = text => [...text.matchAll(/Owner profile \(resident; version ([^;]+);/g)].at(-1)?.[1];
export function apply(ctx) {
  const done = new Set(), boot = process.env.MEM_TEST_BOOT;
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null, tools: (options.tools ?? []).map(t => t.name) });
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: nothing in flight.' };
      else if (!options.tools?.length || options.sessionId !== 'mem-root') block = { type: 'text', text: 'Fixture session' };
      else if (boot === '1') block = (text.includes('wake.json') ? once('woken', () => ({ type: 'text', text: 'WOKEN1 ' + (text.includes('PROFILE_MARKER_V1') ? 'HAS_V1' : 'NO_V1') })) : undefined)
        ?? once('p1', () => tool('p1', 'memory_profile_update', { text: 'PROFILE_MARKER_V1', expected_version: lastVersion(text), reason: 'boot 1' }))
        ?? once('n1', () => tool('n1', 'memory_note', { text: 'NOTE_MARKER_42 the owner wants a ledger' }))
        ?? once('s1', () => tool('s1', 'memory_search', { query: 'NOTE_MARKER_42' }))
        ?? once('d1', () => tool('d1', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? { type: 'text', text: 'Director idle.' };
      else block = (text.includes('wake3.json') ? once('woken3', () => ({ type: 'text', text: 'WOKEN3 ' + (text.includes('PROFILE_MARKER_V2') ? 'HAS_V2' : 'NO_V2') })) : undefined)
        ?? (text.includes('wake2.json') ? once('s2', () => tool('s2', 'memory_search', { query: 'NOTE_MARKER_42' })) : undefined)
        ?? (done.has('s2') ? once('p2', () => tool('p2', 'memory_profile_update', { text: 'PROFILE_MARKER_V2', expected_version: lastVersion(text), reason: 'boot 2' })) : undefined)
        ?? (done.has('p2') ? once('d2', () => tool('d2', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 })) : undefined)
        ?? { type: 'text', text: 'Director idle.' };
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
    if (session.id === 'mem-root' && ['tool/result', 'user/message', 'assistant/message', 'compaction/end'].includes(event.type)) void post('/test/event', event);
  });
}
`;

test('real pinned DSH, two boots: the profile reaches the post-compaction snapshot, memory survives the restart', { timeout: 180000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-memory-'));
  const root = join(directory, 'director'), memRoot = join(directory, 'memory');
  // The local host tags its own driver (and every job a shell starts) with this
  // root-specific variable (local.mjs localSessionEnv + apply), not an AItelier-
  // launched-driver variable - the fixture drives director-local with this root.
  const sessionEnv = localSessionEnv(root);
  const events = [], requests = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const json = id => JSON.parse(result(id).data.message.content.map(b => b.text ?? '').join(''));
  const said = text => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? ''])) + '\n' + output.slice(-3000));
  };
  // Read the tag off the processes the fixture actually creates, never off a comment.
  const environ = pid => { try { return readFileSync(`/proc/${pid}/environ`, 'latin1').split('\0'); } catch { return []; } };
  const tagged = pid => environ(pid).includes(`${sessionEnv}=mem-root`);
  const alive = pid => { try { return readFileSync(`/proc/${pid}/stat`, 'latin1').replace(/^.*\) /s, '')[0] !== 'Z'; } catch { return false; } };
  const helperPids = () => readdirSync('/proc').filter(n => /^\d+$/.test(n)).map(Number)
    .filter(pid => alive(pid) && tagged(pid) && environ(pid).includes(`${INTERNAL_ENV}=${INTERNAL_VALUE}`)).sort((a, b) => a - b);
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-memory-provider.mjs'), PROVIDER);
    // The host's openMemory does not forward a `grepHelper` option, so inject it at the
    // store boundary from a fixture plugin loaded before the director opens its memory:
    // the resident helper then carries the host's tag as well as its internal marker.
    await writeFile(join(directory, 'fixture-grep-tag.mjs'), `\
import { MemoryStore } from ${JSON.stringify(join(here, '../dsh-director/lib/memory/store.mjs'))};
const open = MemoryStore.open;
MemoryStore.open = function (options) {
  return open.call(this, { ...options, grepHelper: { ...(options.grepHelper ?? {}),
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', ${JSON.stringify(sessionEnv)}: 'mem-root' } } });
};
export const name = 'fixture-grep-tag';
export const inject = [];
export function apply() {}
`);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture-grep-tag', name: join(directory, 'fixture-grep-tag.mjs') },
        { id: 'fixture', name: join(directory, 'fixture-memory-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'mem-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          dormantCompactMinTokens: 0, memory: { userId: 'owner', root: memRoot } } }] },
    ]));
    const environment = withoutHostWiring(process.env);
    const launch = boot => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
        // The local host's own dynamic session tag (local.mjs sets it on this driver).
        // The resident memory grep helper carries this tag too, but idle_dormant must not
        // count it as a job the agent started: what excludes it is the runtime's internal
        // marker (core.mjs INTERNAL_ENV / backgroundProcesses), not the tag.
        [sessionEnv]: 'mem-root',
        DSH_HOME: join(directory, 'home'), MEM_TEST_DSH_ENTRY: entry, MEM_TEST_BOOT: boot, MEM_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };
    const compactions = () => events.filter(e => e.type === 'compaction/end' && !e.data.error).length;

    // ---- boot 1
    proc = launch('1');
    await until('child DSH never carried the host session tag', () => tagged(proc.pid));
    await until('boot 1 never went dormant', () => result('d1'));
    // The helper is alive at the very call idle_dormant must accept, and it carries BOTH
    // the host tag and the internal marker - read off /proc, not from a comment.
    const helpersAtDormant = helperPids();
    t.diagnostic(`child pid=${proc.pid} carries ${sessionEnv}=mem-root: ${tagged(proc.pid)}; ` +
      `tagged internal grep helper pids at the dormant call: ${JSON.stringify(helpersAtDormant)}`);
    assert.ok(helpersAtDormant.length, 'the tagged, internally-marked grep helper is alive at the dormant call');
    assert.equal(json('d1').dormant, true, 'idle_dormant was refused: ' + JSON.stringify(json('d1')));
    assert.equal(json('p1').updated, true, JSON.stringify(json('p1')));
    assert.match(json('n1').id, /^n-/);
    const s1 = json('s1');
    // No ownerLanguage in this host's config: memory answers in English (stranger C).
    assert.equal(s1.grep.source, 'grep · literal match · local memory log');
    assert.equal(s1.grep.hits[0].id, json('n1').id);
    // Step 21: the whole entry, dated, once; the ranked answer points at grep's copy.
    assert.equal(s1.grep.hits[0].shown, 'full');
    assert.match(s1.grep.text, new RegExp(`^\\[\\d{4}-\\d{2}-\\d{2} (Sun|Mon|Tue|Wed|Thu|Fri|Sat) \\d{2}:\\d{2}\\+00:00\\] steward \\(note\\) \\{${json('n1').id}\\}: NOTE_MARKER_42 the owner wants a ledger$`));
    assert.equal(s1.ranked.source, 'bm25 · local index');
    assert.deepEqual(s1.ranked.hits.map(h => [h.id, h.shown]), [[json('n1').id, 'see_grep']]);
    assert.match(s1.ranked.text, / → see grep$/);
    assert.match(s1.budget, /^\d+\/6000 chars · omitted 0$/);
    const listed = requests.find(r => r.session === 'mem-root' && r.tools.length)?.tools ?? [];
    assert.ok(!listed.some(name => name.startsWith('memory_')), 'registered, not presented: ' + JSON.stringify(listed));
    await until('dormancy did not compact', () => compactions() >= 1);
    await writeFile(join(root, 'inbox', 'wake.json'), JSON.stringify({ wake: 1 }));
    await until('boot 1 never woke', () => said('WOKEN1'));
    assert.ok(said('WOKEN1 HAS_V1'), 'the post-compaction snapshot lacks the updated profile');
    proc.kill('SIGTERM'); await once(proc, 'exit');

    // ---- boot 2, same session
    const before = compactions();
    proc = launch('2');
    await writeFile(join(root, 'inbox', 'wake2.json'), JSON.stringify({ wake: 2 }));
    await until('boot 2 never searched', () => result('s2'));
    assert.equal(json('s2').grep.hits[0].id, json('n1').id, 'the note survived the restart');
    assert.ok(json('s2').grep.text.endsWith(': NOTE_MARKER_42 the owner wants a ledger'), 'whole, after the restart too');
    await until('boot 2 never went dormant', () => result('d2'));
    assert.equal(json('p2').updated, true, 'the version from the last snapshot was current: ' + JSON.stringify(json('p2')));
    await until('boot 2 dormancy did not compact', () => compactions() > before);
    await writeFile(join(root, 'inbox', 'wake3.json'), JSON.stringify({ wake: 3 }));
    await until('boot 2 never woke', () => said('WOKEN3'));
    assert.ok(said('WOKEN3 HAS_V2'), 'the second boot\'s post-compaction snapshot lacks the updated profile');
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/);

    // The repository holds the history: one commit per write, made by the runtime's git.
    const repo = join(memRoot, 'owner');
    const log = spawnSync('git', ['-c', 'safe.directory=' + repo, 'log', '--format=%s'], { cwd: repo, encoding: 'utf8',
      env: { PATH: process.env.PATH, HOME: repo, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } }).stdout.trim().split('\n');
    assert.deepEqual(log.filter(s => !s.startsWith('init')).map(s => s.replace(/ n-[0-9a-f]+/, ' n-*')),
      ['profile (steward): boot 2', 'note n-* (steward)', 'profile (steward): boot 1']);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
