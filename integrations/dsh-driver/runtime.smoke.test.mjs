import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, realpath } from 'node:fs/promises';
import { tmpdir, homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readHistory } from './history.mjs';
import { stateInjectionSource } from './runtime.mjs';

function assertInjectionCadence(events) {
  let epoch = 0;
  const injected = new Set();
  for (const event of events) {
    if (event.type === 'compaction/end' && !event.data.error) epoch = event.seq;
    if (event.type !== 'user/message') continue;
    if (event.data.source?.kind === stateInjectionSource) {
      assert.equal(event.data.source.compaction_seq, epoch);
      assert.ok(!injected.has(epoch), `State was injected twice in compaction epoch ${epoch}`);
      injected.add(epoch);
      const state = JSON.parse(event.data.content[0].text.split('\n').slice(1).join('\n'));
      assert.deepEqual(Object.keys(state).sort(), ['driver_id','nodes','private_note','project_id','project_note']);
    }
    assert.ok(!event.data.source?.sections?.some(s => s.name === 'aitelier:state') || event.data.source.kind === stateInjectionSource,
      'State was repeated through dynamic native runtime context');
  }
  assert.ok(injected.has(0), 'first request never received State');
  assert.ok(injected.has(epoch), 'latest successful compaction was not followed by fresh State');
}

test('real pinned DSH: native child, auto compact, fresh injection, raw history grep and automatic notifications', { timeout: 45000 }, async () => {
  // Use non-temporary roots: /tmp is writable independently of workspace policy.
  const directory = await mkdtemp(join(homedir(), 'dsh-native-smoke-'));
  const outsideDirectory = await mkdtemp(join(homedir(), 'dsh-outside-workspace-'));
  const childDirectory = join(directory, 'work', 'child');
  await mkdir(childDirectory, {recursive:true});
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const here = dirname(fileURLToPath(import.meta.url));
  const requests = [], activity = [], events = [], workCalls = [], dispatches = [], parked = new Set(), childWaits = new Set();
  let changed = false, overflowChanged = false, contextReads = 0, output = '', proc;
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
      else if (body.method === 'tools/list') result = { tools: ['list_pipelines', 'state_graph_read', 'state_graph_write', 'wait_for_run', 'wait_for_driver_inbox'].map(name => ({ name, description: name,
        inputSchema: { type: 'object', properties: {}, additionalProperties: true } })) };
      else result = { content: [{ type: 'text', text: 'fixture_pipeline' }] };
      return send({ jsonrpc: '2.0', id: body.id, result });
    }
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [{subject:'BOOT_PROJECT_INBOX',status:'unread'}] } });
    if (req.url.endsWith('/context')) { contextReads++; return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: changed ? 'VERIFIED' : 'PENDING' }], project_note: { index: ['project rule'] },
      private_note: { index: [overflowChanged ? 'overflow fresh private rule' : changed ? 'fresh private rule' : 'original private rule'] }, driver_skill: 'fixture driver skill', runtime_skill: 'Automatic notifications.' }); }
    if (req.url.endsWith('/activity')) {
      activity.push(body);
      if (body.phase === 'waiting') for (const target of childWaits) { target.setHeader('content-type','application/json'); target.end('{}'); childWaits.delete(target); }
      return send({ ok: true });
    }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url.endsWith('/work')) { workCalls.push(body); return send({status:'fixture-'+body.action,workspace:'/fixture/owned-child'}); }
    if (req.url === '/api/state/query/wait_for_state_change') {
      if (changed && body.after < 11) return send({ timed_out: false, next_after: 11, next_inbox_after: 1, events: [{ type: 'node_changed' }] });
      parked.add(res); res.on('close', () => parked.delete(res)); return;
    }
    if (req.url === '/test/parent-waiting') {
      if (activity.some(a => a.phase === 'waiting')) return send({ok:true});
      childWaits.add(res); return;
    }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/dispatch') { dispatches.push(body); return send({ok:true}); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/overflow') { overflowChanged = true; return send({ok:true}); }
    if (req.url === '/test/change') {
      changed = true;
      for (const target of parked) { target.setHeader('content-type', 'application/json'); target.end(JSON.stringify({ timed_out: false, next_after: 11, next_inbox_after: 1, events: [{ type: 'node_changed' }] })); }
      parked.clear(); return send({ ok: true });
    }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true }, { id: 'compaction-basic', config: { thresholdRatio: 0.8, retainTokens: 128, headroomTokens: 512 } },
      { insert: [{ id: 'fixture', name: join(here, 'fixture-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') },
        { id: 'mcp-aitelier', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'aitelier', transport: 'streamable-http', url: base + '/mcp', failOnStartupError: true } }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    const environment = { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_TEST_CHILD_CWD: childDirectory,
      AITELIER_TEST_OUTSIDE_CWD: outsideDirectory,
    };
    const launch = () => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: environment });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };
    proc = launch();
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline && proc.exitCode === null) {
      if (activity.some(a => a.phase === 'failed')) break;
      if (events.some(e => e.type === 'tool/result' && JSON.stringify(e.data).includes('next_after_seq'))
          && events.some(e => e.data?.message?.toolCallId === 'fixture-child-shell') && activity.some(a => a.phase === 'waiting' && a.subagents.length === 0)
          && requests.some(r=>!r.child&&!r.summary&&JSON.stringify(r.options.messages).includes('Native child completed.'))) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(proc.exitCode, null, output);
    assert.ok(activity.some(a => a.subagents.length > 0), 'native child activity missing\n' + output + JSON.stringify({contextReads,activity,requests:requests.map(r=>({summary:r.summary,child:r.child})),events:events.map(e=>e.type),directory}));
    assert.ok(events.some(e => e.type === 'compaction/end' && !e.data.error), 'automatic compaction missing\n' + output + JSON.stringify({requests:requests.map(r=>({summary:r.summary,child:r.child,size:JSON.stringify(r.options).length})),events:events.map(e=>({type:e.type,data:e.type==='compaction/end'?e.data:undefined}))}));
    assert.ok(contextReads >= 2, 'context was not refreshed\n' + output);
    assert.ok(requests.some(r => !r.summary && !r.child && JSON.stringify(r.options).includes('你是 alpha 项目 State DAG 的常驻导演')), 'project director goal missing at startup');
    const after = requests.filter(r => !r.summary && !r.child).slice(2);
    assert.ok(after.some(r => JSON.stringify(r.options.messages).includes('fresh private rule')), 'fresh private note missing\n' + output);
    assert.ok(after.some(r => JSON.stringify(r.options.messages).includes('VERIFIED')), 'fresh node status missing');
    assert.ok(after.some(r => JSON.stringify(r.options).includes('你是 alpha 项目 State DAG 的常驻导演')), 'standing director goal missing after compact');
    assert.equal(await readFile(join(childDirectory, 'worker-proof.txt'), 'utf8'), 'WORKER_SHELL_OK', 'native child could not write in its owned checkout under the parent workspace');
    assert.match(await readFile(join(childDirectory, 'commit-proof.txt'), 'utf8'), /^[0-9a-f]{40}\n$/, 'native child could not commit using its own git metadata');
    await assert.rejects(readFile(join(outsideDirectory, 'outside-probe.txt')), {code:'ENOENT'});
    assert.ok(events.some(e => e.data?.message?.toolCallId === 'fixture-child-shell' && JSON.stringify(e.data).includes('OUTSIDE_WRITE_DENIED')), 'child cwd was incorrectly treated as a new write grant');
    assert.ok(events.some(e => e.type === 'tool/result' && JSON.stringify(e.data).includes('next_after_seq') && JSON.stringify(e.data).includes('goal_round')), 'precompact goal history grep missing\n' + output);
    assert.ok(activity.some(a => a.phase === 'waiting' && a.subagents.length > 0), 'parent never waited while the native child was active');
    assert.ok(activity.some(a => a.phase === 'waiting' && a.subagents.length === 0), output);
    assertInjectionCadence(events);
    assert.ok(events.filter(e => e.type === 'compaction/end' && !e.data.error).length >= 2, 'pressure and overflow recovery were not both exercised');
    assert.ok(events.some(e => e.type === 'user/message' && e.data.source?.kind === stateInjectionSource && JSON.stringify(e.data).includes('overflow fresh private rule')), 'overflow retry did not get fresh State');
    const created = events.filter(e => e.type === 'goal/change' && e.data.operation === 'create');
    assert.equal(created.length, 1, 'bootstrap must create exactly one native goal');
    assert.match(created[0].data.goal.objective, /常驻导演/);
    assert.equal(events.filter(e => e.type === 'user/message' && e.data.source?.kind === 'goal').length, 1,
      'idle director immediately spun another native goal round');
    assert.ok(!events.some(e=>e.type==='user/message'&&e.data.source?.kind==='user'), 'bootstrap manufactured a human input');
    const rootRequests=requests.filter(r=>!r.child&&!r.summary&&r.options.tools?.length);
    assert.match(JSON.stringify(rootRequests[0].options.messages), /<goal_round>/,
      'a pending project inbox displaced the initial native goal round');
    assert.ok(rootRequests.some(r=>JSON.stringify(r.options.messages).includes('BOOT_PROJECT_INBOX')),
      'native goal bootstrap lost the pending project inbox\n'+output);
    assert.ok(rootRequests.every(r=>JSON.stringify(r.options).includes('Native DSH driver goal (') && JSON.stringify(r.options).includes('fixture driver skill')),
      'goal or operating protocol disappeared after compact/overflow');
    assert.ok(!requests.filter(r=>r.child&&!r.summary).some(r=>JSON.stringify(r.options).includes('Native DSH driver goal (')),
      'native child inherited the director goal');
    assert.deepEqual(workCalls.map(c => c.action), ['open', 'write', 'bash', 'deliver']);
    assert.ok(workCalls.every(c => c.session_id && c.session_id !== 'fixture-root'), 'child work used the root session identity');
    assert.equal(new Set(workCalls.map(c => c.session_id)).size, 1);
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity) + output);
    const tools = requests.find(r => r.options.tools?.some(t => t.name === 'state_read')).options.tools.map(t => t.name);
    for (const hidden of ['state_graph_read', 'wait_for_run', 'wait_for_driver_inbox']) assert.ok(!tools.includes('mcp__aitelier__' + hidden));
    assert.ok(tools.includes('search_tools') && tools.includes('call_tool'));
    assert.ok(!tools.includes('wait'), 'manual wait tool is still exposed');
    assert.ok(requests.filter(r=>!r.summary && r.options.tools?.length).every(r=>!r.options.tools.some(t=>t.name.startsWith('mcp__'))), 'root or child still receives full MCP schemas');
    for (const child of [false,true]) {
      const relevant = requests.filter(r=>r.child===child && !r.summary);
      assert.ok(relevant.some(r=>JSON.stringify(r.options).includes('agentmcp image')), 'root or child did not receive tool-search usage instructions');
      assert.ok(relevant.some(r=>r.options.messages.some(m=>m.role==='tool' && m.content.some(b=>b.type==='text' && b.text.includes('"parameters"') && b.text.includes('mcp__aitelier__list_pipelines')))), 'search did not return the full matching schema');
      assert.ok(dispatches.some(d=>(d.session_id!=='fixture-root')===child && d.nested && !d.isError), 'MCP dispatch used the wrong caller');
    }
    assert.ok(requests.some(r => !r.child && JSON.stringify(r.options.messages).includes('CHILD_MESSAGE_MARKER')), 'native child messaging missing');
    assert.ok(requests.some(r=>!r.child&&!r.summary&&JSON.stringify(r.options.messages).includes('Native child completed.')),'worker completion did not notify the idle parent');
    assert.ok(events.some(e => e.type === 'tool/result' && e.data.message.toolCallId === 'fixture-child-shell' && !e.data.message.isError && JSON.stringify(e.data.message.content).includes('WORKER_SHELL_OK')), 'native worker shell failed\n' + JSON.stringify(events.filter(e => e.data?.message?.toolCallId === 'fixture-child-shell')));
    assert.ok(events.some(e => e.type === 'tool/result' && JSON.stringify(e.data).includes('fixture_pipeline')), 'pipeline bridge missing');
    const before = requests.length;
    assert.ok(activity.some(a => a.event?.type === 'tool/result'), 'native history events were not published live');
    proc.kill('SIGTERM'); await once(proc, 'exit');
    const archive = await readHistory(join(directory, 'home/sessions'), 'fixture-root', { limit: 100 });
    assert.ok(archive.entries.some(e => e.text.includes('HISTORY_BEFORE_COMPACT_ANCHOR')), 'precompact history lost');
    assert.ok(archive.entries.some(e => e.type === 'compaction/end'));
    assert.ok(archive.entries.every(e => e.timestamp > 0));
    const older = await readHistory(join(directory, 'home/sessions'), 'fixture-root', { before_seq: archive.entries.at(-1).seq, limit: 2 });
    assert.equal(older.entries.length, 2);
    assert.ok(older.entries.at(-1).seq < archive.entries.at(-1).seq);
    // Migrate this exact persisted workspace-write session, not a fresh root.
    environment.AITELIER_DSH_EXECUTION = 'host';
    environment.DSH_PERMISSION_MODE = 'danger-full-access';
    proc = launch();
    const resumedBy = Date.now() + 20000;
    while (Date.now() < resumedBy && proc.exitCode === null && !events.slice().reverse().some(e => e.data?.message?.toolCallId === 'fixture-child-shell' && JSON.stringify(e.data).includes('OUTSIDE_WRITE_ALLOWED'))) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(proc.exitCode, null, output);
    const resumed = requests.slice(before).find(r => r.host && r.options.tools?.some(t => t.name === 'state_read'));
    assert.ok(resumed, 'persisted driver did not resume\n' + output);
    assert.match(JSON.stringify(resumed.options.messages), /earlier details can be searched/);
    assert.match(JSON.stringify(resumed.options), /你是 alpha 项目 State DAG 的常驻导演/);
    assert.equal(events.filter(e=>e.type==='goal/change'&&e.data.operation==='create').length,1,'restart replaced the persisted goal');
    const rearmed=events.find(e=>e.type==='goal/change'&&e.data.operation==='resume');
    assert.equal(rearmed?.data.goal.id,created[0].data.goal.id,'active goal was not rearmed after restart');
    assert.ok(!resumed.options.tools.some(t => t.name === 'project_work'), 'host kept obsolete coding bridge');
    assert.equal(await readFile(join(outsideDirectory, 'root-host-proof.txt'), 'utf8'), 'ROOT_HOST_OK');
    assert.equal(await readFile(join(outsideDirectory, 'outside-probe.txt'), 'utf8'), 'allowed');
    assert.match(await readFile(join(outsideDirectory, 'host-git-proof.txt'), 'utf8'), /^[0-9a-f]{40}\n$/);
    if (process.env.AITELIER_TEST_DOCKER) assert.match(await readFile(join(outsideDirectory, 'docker-proof.txt'), 'utf8'), /^\d+\./);
    const childRequest = requests.slice(before).find(r => r.host && r.child && r.options.tools?.some(t=>t.name==='bash'));
    assert.match(JSON.stringify(childRequest.options), /danger-full-access/);
    assert.doesNotMatch(JSON.stringify(childRequest.options), /Current DSH file policy: workspace-write/);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of [...parked, ...childWaits]) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true });
    await rm(outsideDirectory, { recursive: true, force: true });
  }
});

test('real pinned DSH default host adapter calls the authenticated completion bridge', { timeout: 20000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-host-model-'));
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const here = dirname(fileURLToPath(import.meta.url));
  const completions = [], activity = [];
  let output = '', proc, inboxWait, replied = false, call = 0, heldCompletion, waitRequests = 0, failNext = false;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'host-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url.endsWith('/context')) return send({ nodes: [], project_note: {}, private_note: {}, event_seq: 1 });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url.endsWith('/list_director_messages')) return send({ items: [] });
    if (req.url.endsWith('/wait_for_state_change')) {
      assert.equal(body.timeout_seconds, 240);
      if (++waitRequests === 1) { res.destroy(); return; }
      if (waitRequests === 2) return send({timed_out:true,next_after:1,next_inbox_after:0});
      inboxWait = res; return;
    }
    if (req.url.endsWith('/completion')) {
      assert.equal(req.headers['x-aitelier-driver-token'], 'fixture-only');
      completions.push(body);
      if(failNext){failNext=false;res.statusCode=400;return send({detail:'fixture completion failure'});}
      const hasDriver = body.tools?.some(t => t.function.name === 'state_read');
      assert.ok(!body.tools?.some(t=>t.function.name==='wait'),'manual wait schema leaked');
      const human = hasDriver && JSON.stringify(body.messages).includes('HUMAN_MESSAGE_MARKER');
      const second = hasDriver && JSON.stringify(body.messages).includes('HUMAN_SECOND_MESSAGE');
      const issue = hasDriver && JSON.stringify(body.messages).includes('ISSUE_NOTIFICATION_MARKER');
      const recovered = hasDriver && JSON.stringify(body.messages).includes('RECOVERY_NOTIFICATION_MARKER');
      if (human) replied = true;
      const reply = { text: recovered ? 'RECOVERED_REPLY_MARKER' : issue ? 'ISSUE_HANDLED_MARKER' : second ? 'HUMAN_SECOND_REPLY' : human ? 'HUMAN_REPLY_MARKER' : hasDriver ? 'Idle director.' : 'Fixture title', reasoning_content: 'ready', truncated: false,
        tool_calls: [],
        usage: { prompt_tokens: 100, completion_tokens: 10 } };
      if (human && !second) { heldCompletion = () => send(reply); return; }
      return send(reply);
    }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([{ id: 'hmr', disabled: true }, { insert: [{ id: 'driver', name: join(here, 'runtime.mjs') }] }]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'host-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'host-root',
      AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'absent'),
    } });
    proc.stdout.on('data', c => output += c); proc.stderr.on('data', c => output += c);
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline && proc.exitCode === null && !activity.some(a => a.phase === 'waiting')) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(activity.some(a => a.phase === 'waiting'), output + JSON.stringify(activity));
    assert.ok(completions.some(c => c.session_id === 'host-root' && c.tools.some(t => t.function.name === 'state_read')), JSON.stringify(completions));
    while (Date.now() < deadline && !inboxWait) await new Promise(resolve => setTimeout(resolve, 50));
    const errorIndex = activity.findIndex(a => a.error?.includes('AItelier event subscription: fetch failed'));
    assert.ok(errorIndex >= 0, 'transport error was not diagnosed');
    assert.ok(activity.slice(errorIndex + 1).some(a => !a.error && a.phase === 'waiting'), 'successful empty response did not clear connection error');
    await new Promise(resolve => setTimeout(resolve, 400));
    assert.equal(completions.filter(c => c.tools?.some(t => t.function.name === 'state_read')).length, 1, 'idle active goal must not generate an immediate continuation');
    assert.ok(inboxWait, 'combined runtime wait missing');
    const firstWait = inboxWait;
    inboxWait.setHeader('content-type', 'application/json');
    inboxWait.end(JSON.stringify({timed_out: false, next_after: 1, next_inbox_after: 1,
      driver_inbox: [{body: 'HUMAN_MESSAGE_MARKER', status: 'unread'}]}));
    const busyDeadline = Date.now() + 4000;
    while (Date.now() < busyDeadline && (!heldCompletion || inboxWait === firstWait)) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(heldCompletion && inboxWait !== firstWait, 'model call was not held while the subscription re-armed');
    inboxWait.setHeader('content-type', 'application/json');
    inboxWait.end(JSON.stringify({timed_out: false, next_after: 1, next_inbox_after: 2,
      driver_inbox: [{body: 'HUMAN_SECOND_MESSAGE', status: 'unread'}]}));
    let queued = false;
    while (Date.now() < busyDeadline && !queued) {
      try { queued = JSON.parse(await readFile(join(directory, 'cursor.json'), 'utf8')).inboxAfter === 2; } catch {}
      if (!queued) await new Promise(resolve => setTimeout(resolve, 25));
    }
    assert.ok(queued, 'second inbox message was not persisted during active generation');
    heldCompletion();
    const replyDeadline = Date.now() + 8000;
    while (Date.now() < replyDeadline && !activity.some(a => a.event?.text.includes('HUMAN_SECOND_REPLY'))) await new Promise(resolve => setTimeout(resolve, 50));
    assert.ok(replied, 'inbox message never reached the native model');
    assert.ok(activity.some(a => a.event?.text.includes('HUMAN_REPLY_MARKER')), output + JSON.stringify(activity));
    assert.ok(activity.some(a => a.event?.text.includes('HUMAN_SECOND_REPLY')), 'busy-time input was not consumed on the next step');
    await new Promise(resolve => setTimeout(resolve, 400));
    assert.equal(completions.filter(c => c.tools?.some(t => t.function.name === 'state_read')).length, 3, 'ordinary transport timeouts caused extra model turns');
    // A new project issue wakes an agent that has ALREADY finished its turn.
    const issueWait = inboxWait;
    issueWait.setHeader('content-type', 'application/json');
    issueWait.end(JSON.stringify({timed_out:false,next_after:2,next_inbox_after:2,
      events:[{seq:2,project_id:'alpha',event_type:'issue_reported',payload:{issue_id:'iss-new',kind:'defect',title:'ISSUE_NOTIFICATION_MARKER'}}]}));
    const issueDeadline=Date.now()+5000;
    while(Date.now()<issueDeadline && !activity.some(a=>a.event?.text.includes('ISSUE_HANDLED_MARKER'))) await new Promise(resolve=>setTimeout(resolve,25));
    assert.ok(activity.some(a=>a.event?.text.includes('ISSUE_HANDLED_MARKER')),'new issue did not wake an idle driver');
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(completions.filter(c=>c.tools?.some(t=>t.function.name==='state_read')).length,4,'issue notification caused an idle spin');
    const stateEvents = activity.filter(a => a.event?.type === 'user/message' && JSON.parse(a.event.text).source?.kind === stateInjectionSource);
    assert.equal(stateEvents.length, 1, 'ordinary inbox messages duplicated the initial State snapshot');
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity));
    // Model failures must stay visible across successful empty subscription
    // replies, then recover when a later notification starts a successful turn.
    failNext=true;
    inboxWait.setHeader('content-type','application/json');
    inboxWait.end(JSON.stringify({timed_out:false,next_after:3,next_inbox_after:2,
      events:[{seq:3,event_type:'node_revised',payload:{reason:'trigger one failed completion'}}]}));
    const failedBy=Date.now()+4000;
    while(Date.now()<failedBy&&!activity.some(a=>a.phase==='failed'&&a.error?.includes('fixture completion failure')))await new Promise(resolve=>setTimeout(resolve,25));
    assert.ok(activity.some(a=>a.phase==='failed'&&a.error?.includes('fixture completion failure')),'model failure was not reported');
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(activity.at(-1).phase,'failed','idle event hid a model failure');
    const oldWait=inboxWait;
    oldWait.setHeader('content-type','application/json');oldWait.end(JSON.stringify({timed_out:true,next_after:3,next_inbox_after:2}));
    while(Date.now()<failedBy&&inboxWait===oldWait)await new Promise(resolve=>setTimeout(resolve,25));
    assert.equal(activity.at(-1).phase,'failed','an empty subscription reply hid the model failure');
    inboxWait.setHeader('content-type','application/json');
    inboxWait.end(JSON.stringify({timed_out:false,next_after:4,next_inbox_after:2,
      events:[{seq:4,event_type:'issue_reported',payload:{issue_id:'iss-recover',title:'RECOVERY_NOTIFICATION_MARKER'}}]}));
    const recoveredBy=Date.now()+4000;
    while(Date.now()<recoveredBy&&!activity.some(a=>a.event?.text.includes('RECOVERED_REPLY_MARKER')))await new Promise(resolve=>setTimeout(resolve,25));
    assert.ok(activity.some(a=>a.event?.text.includes('RECOVERED_REPLY_MARKER')),'later notification did not recover the idle failed agent');
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(activity.at(-1).phase,'waiting');assert.equal(activity.at(-1).error,'');
  } finally {
    if (proc?.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
