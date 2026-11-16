/** The role table in a real pinned DSH, across three boots of ONE session log.
 *
 * Boot 1 serves turns under the repository roles.json. Between boots 1 and 2 the
 * table changes (a tool leaves the worker's presented list for its forbidden list,
 * another leaves the director's presented list). Boot 2 must load the log boot 1
 * wrote and serve a turn under the NEW table; boot 3 must load what boot 2 wrote and
 * serve a turn. This is the shape that bricked both production drivers on
 * 2026-11-15 00:39Z (a request/header appended at boot, outside a turn: DSH accepts
 * it, then refuses to load the log on the NEXT start), applied to a role-table
 * change instead of an execution-mode change (runtime.smoke.test.mjs keeps that one).
 *
 * Every boot also checks the role split itself: the director's requests carry
 * exactly its presented tools and driver.md, the child's exactly the worker's and
 * worker.md, and the child is refused `subagent` through call_tool and by name. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, readdir, copyFile, rm, cp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { parseRoles } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';
import { watchProgress } from './progress.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const systemText = request => request.options.messages.filter(m => m.role === 'system')
  .map(m => typeof m.content === 'string' ? m.content : m.content.map(b => b.text ?? '').join('')).join('\n');
const toolNames = request => request.options.tools.map(t => t.name);
/** The tool-result messages answering one call id, as JSON text. */
const resultFor = (requests, id) => requests.flatMap(r => r.options.messages)
  .filter(m => m.role === 'tool' && JSON.stringify(m).includes(id)).map(m => JSON.stringify(m)).join('\n');

test('real pinned DSH: the role split holds, and a roles.json change between boots of one session log loads and serves on the next two boots', { timeout: Infinity }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const directory = await mkdtemp(join(tmpdir(), 'dsh-roles-'));
  // A private copy of the runtime, so this test can change ITS roles.json between
  // boots; production has no knob for that and gets none.
  // The adapter imports the director plugin as ../dsh-director, so the copy keeps that layout.
  const plugin = join(directory, 'plugin', 'dsh-driver');
  await mkdir(plugin, { recursive: true });
  await cp(join(here, '..', 'dsh-director'), join(directory, 'plugin', 'dsh-director'), { recursive: true, filter: source => !source.includes('node_modules') });
  for (const file of await readdir(here))
    if (/\.(mjs|md|json)$/.test(file) && !file.endsWith('.test.mjs') && file !== 'package-lock.json') await copyFile(join(here, file), join(plugin, file));
  // Since plugin step 4 each role prompt has a generic half (dsh-director) and AItelier's half (here).
  const director = join(directory, 'plugin', 'dsh-director');
  const driverSkill = await readFile(join(director, 'director.md'), 'utf8');
  const stateSkill = await readFile(join(plugin, 'state.md'), 'utf8');
  const workerSkill = await readFile(join(director, 'worker.md'), 'utf8');
  const workerStateSkill = await readFile(join(plugin, 'worker-state.md'), 'utf8');
  const requests = [], activity = [], parked = new Set();
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/mcp') {
      if (req.method === 'GET') { res.statusCode = 405; return res.end(); }
      if (req.method === 'DELETE') { res.statusCode = 200; return res.end(); }
      if (!Object.hasOwn(body, 'id')) { res.statusCode = 202; return res.end(); }
      let result;
      if (body.method === 'initialize') result = { protocolVersion: body.params.protocolVersion,
        capabilities: { tools: {} }, serverInfo: { name: 'fixture-aitelier', version: '1' } };
      else if (body.method === 'tools/list') result = { tools: ['list_pipelines', 'state_graph_read', 'state_graph_write', 'wait_for_run', 'wait_for_driver_inbox']
        .map(name => ({ name, description: name, inputSchema: { type: 'object', properties: {}, additionalProperties: true } })) };
      else result = { content: [{ type: 'text', text: 'fixture' }] };
      return send({ jsonrpc: '2.0', id: body.id, result });
    }
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-roles' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { project_id: 'alpha', matched_total: 0, has_more: false, items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-roles', event_seq: 1, nodes: [],
      project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') { parked.add(res); res.on('close', () => parked.delete(res)); return; }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(here, 'fixture-roles-provider.mjs') }, { id: 'director', name: join(directory, 'plugin', 'dsh-director', 'index.mjs') }, { id: 'driver', name: join(plugin, 'runtime.mjs') },
        { id: 'mcp-aitelier', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'aitelier', transport: 'streamable-http', url: base + '/mcp', failOnStartupError: true } }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    // Production's execution mode, chosen here rather than inherited from the shell.
    const environment = { ...withoutCallerMode({ ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-roles', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'roles-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets') }),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access' };

    /** One boot: launch, serve a full director turn around one native child, check the split under `table`, stop. */
    const serveBoot = async (boot, table) => {
      const effective = aitelierRoles(parseRoles(table));
      environment.AITELIER_TEST_BOOT = String(boot);
      proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: environment });
      proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
      const mine = () => requests.filter(r => r.boot === String(boot) && !r.purpose && r.options.tools?.length);
      const roots = () => mine().filter(r => !r.child), children = () => mine().filter(r => r.child);
      const done = 'ROLES_CHILD_DONE boot ' + boot;
      const progress = await watchProgress({
        what: `boot ${boot} to load the session and serve a director turn around one native child`,
        running: () => proc.exitCode === null && !/SessionPersistenceCorruptionError|stored log is corrupt/.test(output),
        failed: () => activity.some(a => a.phase === 'failed'),
        milestones: [
          { name: 'the director served a request', met: () => roots().length > 0 },
          { name: 'the native child served a request', met: () => children().length > 0 },
          { name: 'the child finished its script', met: () => children().some(r => r.options.messages.filter(m => m.role === 'assistant').length >= 3) },
          { name: 'the director took a turn on the child\'s completion', met: () => roots().some(r => JSON.stringify(r.options.messages).includes(done)) },
        ],
      });
      assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/, `boot ${boot} could not load the session log\n` + output.slice(-3000));
      assert.equal(proc.exitCode, null, output.slice(-3000));
      assert.ok(progress.done, progress.message + '\n' + output.slice(-3000));
      // Equality per role, missing names printed (D1/MF7).
      for (const [role, list] of [['director', roots()], ['worker', children()]]) {
        const presented = effective[role].presented;
        for (const r of list) {
          const names = toolNames(r);
          assert.deepEqual(names.filter(n => !presented.includes(n)), [], `boot ${boot}: ${role} request lists tools its role does not present`);
          assert.deepEqual(presented.filter(n => !names.includes(n)), [], `boot ${boot}: ${role} presented names missing from its request`);
        }
      }
      for (const r of children()) for (const name of ['subagent', 'workflow', 'todo_plan'])
        assert.ok(!toolNames(r).includes(name), `boot ${boot}: child request lists ${name}`);
      // Prompts: the worker halves only in the child, the director halves only in the director.
      for (const r of roots()) {
        assert.ok(systemText(r).includes(driverSkill), `boot ${boot}: director system prompt lacks director.md`);
        assert.ok(systemText(r).includes(stateSkill), `boot ${boot}: director system prompt lacks state.md`);
        assert.ok(systemText(r).indexOf(driverSkill) < systemText(r).indexOf(stateSkill), `boot ${boot}: state.md must follow director.md`);
        assert.ok(!systemText(r).includes(workerSkill.trim().split('\n')[0]), `boot ${boot}: director system prompt carries worker.md`);
        assert.ok(!systemText(r).includes(workerStateSkill.trim().split('\n')[0]), `boot ${boot}: director system prompt carries worker-state.md`);
      }
      for (const r of children()) {
        assert.ok(systemText(r).includes(workerSkill), `boot ${boot}: child system prompt lacks worker.md`);
        assert.ok(systemText(r).includes(workerStateSkill), `boot ${boot}: child system prompt lacks worker-state.md`);
        assert.ok(!systemText(r).includes(driverSkill.split('\n')[0]), `boot ${boot}: child system prompt carries director.md`);
        assert.ok(!systemText(r).includes(stateSkill.split('\n')[0]), `boot ${boot}: child system prompt carries state.md`);
      }
      // The guard, in the real loop: subagent through call_tool and by its direct name.
      for (const step of [0, 1]) assert.match(resultFor(children(), `roles-child-${boot}-${step}`), /subagent is the director's/,
        `boot ${boot}: the child was not refused subagent (step ${step})`);
      const jobKill = resultFor(children(), `roles-child-${boot}-2`);
      assert.ok(jobKill, `boot ${boot}: job_kill result missing`);
      return { effective, jobKill, roots: roots() };
    };
    const stop = async () => { proc.kill('SIGTERM'); await once(proc, 'exit'); };

    const tableA = await readFile(join(director, 'roles.json'), 'utf8');
    const first = await serveBoot(1, tableA);
    assert.doesNotMatch(first.jobKill, /job_kill is the director's/, 'job_kill is open to workers under the repository table');
    await stop();

    // The change between boots: job_kill moves from the worker's presented list to
    // its forbidden list; todo_templates leaves the director's presented list.
    const changed = JSON.parse(tableA);
    changed.worker.presented = changed.worker.presented.filter(n => n !== 'job_kill');
    changed.worker.forbidden.push('job_kill');
    changed.director.presented = changed.director.presented.filter(n => n !== 'todo_templates');
    const tableB = JSON.stringify(changed, null, 2);
    await writeFile(join(director, 'roles.json'), tableB);
    const second = await serveBoot(2, tableB);
    assert.match(second.jobKill, /job_kill is the director's/, 'the changed table did not forbid job_kill on the next boot');
    assert.ok(second.roots.every(r => !toolNames(r).includes('todo_templates')), 'the director still lists todo_templates');
    await stop();

    // The third boot loads what the second wrote and serves a turn.
    await serveBoot(3, tableB);
    await stop();
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});
