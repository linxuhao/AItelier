/** Tool discovery, exercised against the real harness libraries.
 *
 * These tests do not boot `dsh`; they import five libraries from the install the
 * driver is pinned to. WHICH install that is belongs to `dsh-entry.mjs`, not to
 * the invoking shell: this header used to read
 *
 *   const require = createRequire(process.env.AITELIER_DSH_TEST_ENTRY
 *     ?? new URL('./package.json', import.meta.url));
 *
 * so an exported AITELIER_DSH_TEST_ENTRY pointed the assertions at some other
 * build, and a clean checkout with no node_modules failed at LOAD time, before
 * any test callback existed to report it.
 *
 * The shape below keeps the asymmetry the resolver already enforces:
 *
 *  - an override it refuses (missing, a directory, a wrong version) throws here,
 *    with the resolver's own words, at load time;
 *  - a machine with nothing installed reports ONE skipped test naming the paths
 *    it looked in and exits 0 — node's runner shows a top-level `t.skip()` as
 *    SKIP, which is the only way to skip before the imports have run;
 *  - anything else — a library missing from the measured install, a broken
 *    export, a failing assertion — propagates untouched and reddens the file.
 *    Nothing here is wrapped in try/catch, so a skip cannot absorb a real error.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installToolDiscovery, searchTools, presentTools } from './tool-discovery.mjs';
import { loadRoles } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { discoveryTexts } from './aitelier-texts.mjs';

/** Generic layer: the repository table alone. Adapter layer: plus AItelier's additions (D3). */
const table = loadRoles();
const effective = aitelierRoles(table);
import { DSH_PACKAGE, describeDeclinedToMeasure, resolveDshLibraries } from './dsh-entry.mjs';

/** The install libraries these tests import; each one must come from the measured install. */
const HARNESS_LIBRARIES = ['@deepseek-ai/cordis', '@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-scope', '@deepseek-ai/dsh-llm'];

const libraries = await resolveDshLibraries();

// A refused override is a request this runner must not answer with another
// install, so it must not be answered with a cheerful skip either.
if (libraries.status === 'refused') throw new Error(libraries.reason);

test('the director presents every runtime tool but the workspace-only project_work; the adapter adds its State write', () => {
  // The runtime's tools are registered by the adapter (runtime.mjs) and the director plugin (dsh-director/index.mjs).
  const registered = ['./runtime.mjs', '../dsh-director/index.mjs'].flatMap(file => [...readFileSync(new URL(file, import.meta.url), 'utf8').matchAll(/defineTool\(\{ name: '([a-z_]+)'/g)].map(m => m[1]));
  assert.ok(registered.length >= 14, 'could not read the registrations: ' + registered.length);
  // reply_to_director is registered for the steward only.
  // family_memory_note / family_memory_show are the steward's, registered only with a family group (step 23).
  assert.deepEqual(registered.filter(name => !['project_work', 'reply_to_director', 'schedule_list', 'schedule_set', 'schedule_delete',
    'family_memory_note', 'family_memory_show'].includes(name) && !effective.director.presented.includes(name)), [], 'a runtime tool would be hidden from the director');
  const tools = ['bash', 'glob', 'web_fetch', 'workflow', 'subagent', 'todo_plan', 'mcp__aitelier__state_graph_write', 'mcp__aitelier__state_graph_read', 'mcp__devmcp__x']
    .map(name => ({ name }));
  assert.deepEqual(presentTools(tools, effective.director).map(t => t.name), ['bash', 'subagent', 'todo_plan', 'mcp__aitelier__state_graph_write']);
  assert.deepEqual(presentTools(tools, effective.worker).map(t => t.name), ['bash']);
  // The generic layer alone never names a State tool.
  assert.deepEqual(presentTools(tools, table.director).map(t => t.name), ['bash', 'subagent', 'todo_plan']);
  assert.deepEqual(effective.director.presented.filter(n => !table.director.presented.includes(n)), ['state_read', 'send_user_message', 'mcp__aitelier__state_graph_write']);
});

test('search ranks exact names and Unicode descriptions, bounds results, and refuses empty queries', () => {
  const schemas = ['run_pipeline', 'get_pipeline', 'list_pipelines'].map(name => ({ name, description: '流水线 pipeline', parameters: { type: 'object' } }));
  assert.equal(searchTools(schemas, 'run_pipeline').tools[0].name, 'run_pipeline');
  assert.equal(searchTools(schemas, 'run pipeline').tools[0].name, 'run_pipeline');
  assert.equal(searchTools(schemas, '流水线', 1).tools.length, 1);
  assert.equal(searchTools(schemas, '流水线', 1).has_more, true);
  assert.equal(searchTools(schemas, 'unknown').total_matches, 0);
  assert.throws(() => searchTools(schemas, '  '), /empty/);
  assert.throws(() => searchTools(schemas, 'pipeline', 11), /1–10/);
});

if (libraries.status === 'unmeasured') {
  // No `t` can exist before the imports below, so the unmeasured fact travels
  // through this one top-level skipping test instead of a thrown load error.
  test('tool discovery against the pinned harness libraries (needs a pinned @deepseek-ai/dsh install to import from)', t => t.skip(libraries.reason));
} else {
  const require = libraries.require;
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
  const { default: SystemPrompt } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'));
  const { default: Tools, defineTool } = await import(require.resolve('@deepseek-ai/dsh-tools'));
  const { createScope } = await import(require.resolve('@deepseek-ai/dsh-scope'));
  const { HarnessError, createUserMessage } = await import(require.resolve('@deepseek-ai/dsh-llm'));
  const output = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: 'rendered:' + JSON.stringify(value) }] };

  test('every library these tests import comes from the install that was measured', (t) => {
    // Not a tautology: node would go on to a parent node_modules if a library
    // were missing here, and that would be a different build than the pin.
    for (const id of HARNESS_LIBRARIES) {
      const resolved = require.resolve(id);
      assert.ok(resolved.startsWith(libraries.nodeModules + '/'), `${id} resolved outside the measured install: ${resolved}`);
    }
    t.diagnostic(`importing ${DSH_PACKAGE} ${libraries.version} (${libraries.source}) libraries from ${libraries.nodeModules}`);
    // A green run must not hide which other installs it declined (see dsh-entry.mjs).
    if (libraries.mismatches.length) t.diagnostic(describeDeclinedToMeasure(libraries.pinned, libraries.mismatches));
  });

  async function fixture(body, roles = effective, rootRole = undefined, options = {}) {
    const ctx = new Context();
    const prompt = ctx.plugin(SystemPrompt); await prompt.await();
    const tools = ctx.plugin(Tools); await tools.await();
    installToolDiscovery(ctx, { defineTool, HarnessError, rootSession: 'root', roles, ...(rootRole ? { rootRole } : {}), ...options });
    let scopeOwner;
    const dependencies = ctx.inject(['tools', 'systemPrompt'], owner => { scopeOwner = owner; }); await dependencies.await();
    const scopes = [];
    const agent = id => {
      const subject = { session: { id } };
      const scope = createScope(scopeOwner, subject); scopes.push(scope); subject.ctx = scope.ctx;
      return subject;
    };
    let sequence = 0;
    const call = (name, args, subject, signal = new AbortController().signal) => ctx.tools.execute({
      name, arguments: args, agent: subject, signal, callId: 'discovery-test-' + ++sequence,
    });
    try { await body({ ctx, agent, call }); }
    finally { for (const scope of scopes.reverse()) await scope.dispose(); await dependencies.dispose(); await tools.dispose(); await prompt.dispose(); }
  }

  test('real registry assembly hides MCP schemas, keeps native tools, and discovers live changes', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root');
    ctx.tools.register(defineTool({ name: 'bash', description: 'native shell', parameters: {}, output, async execute() { return 'native'; } }));
    for (let n = 0; n < 100; n++) ctx.tools.register(defineTool({ name: 'mcp__catalog__tool_' + n,
      description: 'deferred catalog ' + 'unused metadata '.repeat(50), parameters: { number: { type: 'integer', required: true } }, output,
      async execute({number}, exec) { return { number, session: exec.agent.session.id }; },
    }));
    const before = await ctx.systemPrompt.assemble({scope:root});
    assert.deepEqual(before.tools.map(t => t.name).sort(), ['bash', 'call_tool', 'search_tools']);
    assert.ok(JSON.stringify(before.tools).length < JSON.stringify(ctx.tools.schemas(root)).length / 10);
    assert.ok(!JSON.stringify(before.sections).includes('unused metadata'));
    const found = await call('search_tools', {query:'mcp__catalog__tool_37', limit:1}, root);
    assert.equal(found.isError, false); assert.equal(found.value.tools[0].name, 'mcp__catalog__tool_37');
    assert.deepEqual(found.value.tools[0].parameters.required, ['number']);
    const added = ctx.tools.register(defineTool({name:'mcp__live__new_tool',description:'newly connected service',parameters:{},output,async execute(){return 'new';}}));
    assert.equal((await call('search_tools',{query:'newly connected'},root)).value.tools[0].name,'mcp__live__new_tool');
    added();
    assert.equal((await call('search_tools',{query:'newly connected'},root)).value.total_matches,0);
    assert.deepEqual((await ctx.systemPrompt.assemble({scope:root})).tools,before.tools, 'search changed the model tool prefix');
  }));

  test('tiers in the real assembly: deferred built-ins are unlisted but searchable and callable; the State write is listed for the director only', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'), child = agent('child');
    for (const name of ['bash', 'glob', 'web_fetch', 'mcp__aitelier__state_graph_write', 'mcp__aitelier__state_graph_read'])
      ctx.tools.register(defineTool({ name, description: name + ' tool', parameters: {}, output, async execute(_a, exec) { return name + ':' + exec.agent.session.id; } }));
    const listedFor = async subject => (await ctx.systemPrompt.assemble({ agent: subject, scope: subject })).tools.map(t => t.name).sort();
    assert.deepEqual(await listedFor(root), ['bash', 'call_tool', 'mcp__aitelier__state_graph_write', 'search_tools']);
    assert.deepEqual(await listedFor(child), ['bash', 'call_tool', 'search_tools']);
    const found = await call('search_tools', { query: 'glob', limit: 1 }, child);
    assert.equal(found.value.tools[0].name, 'glob', 'a deferred built-in is not searchable');
    assert.equal((await call('call_tool', { name: 'glob', arguments: {} }, child)).value, 'glob:child');
    assert.equal((await call('web_fetch', {}, root)).value, 'web_fetch:root', 'a deferred tool called by its known name');
  }));

  test('root and native child discovery/dispatch use their own restrictions and real caller identities', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'), child = agent('child'), observed = [];
    ctx.tools.register(defineTool({name:'mcp__fixture__identity',description:'caller identity',parameters:{},output,
      async execute(_args, exec){observed.push(exec);return exec.agent.session.id;}}));
    root.ctx.tools.restrict({deny:['mcp__fixture__identity']});
    assert.equal((await call('search_tools',{query:'caller identity'},root)).value.total_matches,0);
    assert.equal((await call('call_tool',{name:'mcp__fixture__identity',arguments:{}},root)).error.info.code,'UNKNOWN_TOOL');
    assert.equal((await call('search_tools',{query:'caller identity'},child)).value.tools[0].name,'mcp__fixture__identity');
    const result = await call('call_tool',{name:'mcp__fixture__identity',arguments:{}},child);
    assert.equal(result.value,'child');assert.deepEqual(result.content,[{type:'text',text:'rendered:"child"'}]);
    assert.equal(observed.length,1);assert.equal(observed[0].agent,child);
    assert.ok(observed[0].parent);assert.equal(observed[0].callId,observed[0].rootCallId+':call_tool');
  }));

  test('call_tool preserves native validation, policy errors, rendered content and deferred context', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'); let executed = 0;
    ctx.tools.register(defineTool({name:'fixture_native',description:'native target',parameters:{number:{type:'integer',required:true}},output,
      async execute({number}, exec){executed++;exec.deferContext(createUserMessage({content:[{type:'text',text:'deferred context'}],source:{kind:'user'}}));exec.concludeTurn();return number*2;}}));
    const invalid = await call('call_tool',{name:'fixture_native',arguments:{number:'wrong'}},root);
    assert.equal(invalid.isError,true);assert.equal(executed,0);assert.match(JSON.stringify(invalid.content),/number/);
    ctx.tools.guard(exec => exec.name === 'fixture_native' && exec.arguments.number === 9 ? 'original policy denial' : undefined);
    const denied = await call('call_tool',{name:'fixture_native',arguments:{number:9}},root);
    assert.equal(denied.isError,true);assert.equal(executed,0);assert.match(JSON.stringify(denied.content),/original policy denial/);
    const result = await call('call_tool',{name:'fixture_native',arguments:{number:3}},root);
    assert.equal(executed,1);assert.equal(result.value,6);assert.equal(result.concludesTurn,true);
    assert.equal(result.additionalContexts.length,1);assert.match(JSON.stringify(result.additionalContexts),/deferred context/);
    assert.deepEqual(result.content,[{type:'text',text:'rendered:6'}]);
    ctx.on('tools/post-execute', async (exec, _result, next) => exec.name === 'call_tool' ? {kind:'accept',content:[{type:'text',text:'outer policy redaction'}]} : next());
    assert.deepEqual((await call('call_tool',{name:'fixture_native',arguments:{number:4}},root)).content,[{type:'text',text:'outer policy redaction'}]);
  }));

  test('nested failures retain error codes and cancellation waits for owned cleanup', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'); let entered, settled = false;
    const started = new Promise(resolve => entered = resolve);
    ctx.tools.register(defineTool({name:'mcp__fixture__fail',description:'failing target',parameters:{},output,async execute(){throw new HarnessError('original failure','FIXTURE_FAILED');}}));
    const failed = await call('call_tool',{name:'mcp__fixture__fail',arguments:{}},root);
    assert.equal(failed.isError,true);assert.equal(failed.error.info.code,'FIXTURE_FAILED');assert.match(JSON.stringify(failed.content),/original failure/);
    ctx.tools.register(defineTool({name:'mcp__fixture__slow',description:'cooperative target',parameters:{},output,
      async execute(_args, exec){entered();await new Promise(resolve=>exec.signal.addEventListener('abort',resolve,{once:true}));await new Promise(resolve=>setTimeout(resolve,10));settled=true;return 'cleaned';}}));
    const controller = new AbortController();
    const pending = call('call_tool',{name:'mcp__fixture__slow',arguments:{}},root,controller.signal);
    await started;controller.abort(new Error('cancel fixture'));
    const result = await pending;assert.equal(settled,true);assert.equal(result.isError,true);assert.equal(result.error.info.code,'ABORTED');
    for (const name of ['call_tool','search_tools','run_code']) assert.equal((await call('call_tool',{name,arguments:{}},root)).isError,true);
  }));

  test('call_tool retains image attachment handles and the target text projection', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root');
    const content = [{type:'text',text:'image caption'}, {type:'image',attachment:{
      attachmentId:'fixture-immutable-image',mediaType:'image/png',bytes:68,width:1,height:1,
    }}];
    ctx.tools.register(defineTool({name:'mcp__fixture__image',description:'stored image handle',parameters:{},
      output:{schema:{type:'string'},render:()=>content},async execute(){return 'stored-image';}}));
    const result = await call('call_tool',{name:'mcp__fixture__image',arguments:{}},root);
    assert.equal(result.isError,false);assert.equal(result.value,'stored-image');assert.deepEqual(result.content,content);
  }));

  test('State writers are refused for any non-root session, through call_tool or directly, and kept for the root', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'), child = agent('child'), executed = [];
    // The real aitelier server's shape: one write tool, the operation in `action`.
    ctx.tools.register(defineTool({name:'mcp__aitelier__state_graph_write',description:'state write',
      parameters:{action:{type:'string'},arguments:{type:'object',additionalProperties:true}},output,
      async execute(args, exec){executed.push([args.action, exec.agent.session.id]);return 'ok';}}));
    const separate = ['declare_driver_presence', 'answer_checkpoint', 'record_evidence', 'verify_node', 'report_external_attempt',
      'start_external_attempt', 'claim_node'];
    for (const name of [...separate, 'run_pipeline'])
      ctx.tools.register(defineTool({name:'mcp__aitelier__' + name,description:'separate tool ' + name,parameters:{},output,
        async execute(_args, exec){executed.push(['tool:' + name, exec.agent.session.id]);return 'ok';}}));
    const write = action => ({name:'mcp__aitelier__state_graph_write',arguments:{action,arguments:{}}});
    const refusedFor = result => { assert.equal(result.isError,true);
      assert.match(JSON.stringify(result.content),/workers do not judge their own work; the director records State evidence/); };
    for (const action of ['record_evidence', 'verify_node', 'report_external_attempt', 'start_external_attempt', 'claim_node']) {
      refusedFor(await call('call_tool',write(action),child));
      refusedFor(await call('mcp__aitelier__state_graph_write',write(action).arguments,child));
      assert.equal((await call('call_tool',write(action),root)).isError,false);
    }
    refusedFor(await call('call_tool',{name:'mcp__aitelier__state_graph_write',arguments:{arguments:{}}},child));
    refusedFor(await call('call_tool',write(7),child));
    // Separate director-only tools: refused for a child through call_tool and directly, kept for the root.
    for (const name of separate) {
      refusedFor(await call('call_tool',{name:'mcp__aitelier__' + name,arguments:{}},child));
      refusedFor(await call('mcp__aitelier__' + name,{},child));
      assert.equal((await call('call_tool',{name:'mcp__aitelier__' + name,arguments:{}},root)).isError,false, name + ' for the root');
    }
    assert.equal((await call('call_tool',{name:'mcp__aitelier__run_pipeline',arguments:{}},child)).isError,false, 'pipeline tools stay open');
    // An allowlist, not a denylist: any action not named is the director's.
    for (const action of ['resolve_issue', 'take_over_claim', 'take_over_attempt', 'abandon_external_attempt', 'release_claim',
      'heartbeat', 'revise_node', 'add_nodes', 'link_issue', 'declare_driver_presence', 'some_future_action']) {
      const result = await call('call_tool',write(action),child);
      refusedFor(result); assert.match(JSON.stringify(result.content),/may only state_graph_write report_issue\)/);
      assert.equal((await call('call_tool',write(action),root)).isError,false);
    }
    assert.equal((await call('call_tool',write('report_issue'),child)).isError,false, 'report_issue is allowlisted for workers');
    assert.deepEqual(executed.filter(([, session]) => session === 'child').map(([action]) => action), ['tool:run_pipeline', 'report_issue']);
    assert.equal(executed.filter(([, session]) => session === 'root').length, 16 + separate.length);
  }));

  // Generic layer (repository roles.json only): every forbidden name is refused for a
  // worker through call_tool AND by its direct name, with the table's reason; the
  // director runs each; presentation follows each role's list exactly.
  test('roles.json: worker forbidden tools are refused via call_tool and by direct name; each role is presented its own list', () => fixture(async ({ctx, agent, call}) => {
    const root = agent('root'), child = agent('child'), ran = [];
    const all = [...new Set([...table.director.presented, ...table.worker.presented, ...table.worker.forbidden, 'glob'])]
      .filter(name => !['search_tools', 'call_tool'].includes(name));
    for (const name of all) ctx.tools.register(defineTool({ name, description: name + ' tool', parameters: {}, output,
      async execute(_a, exec) { ran.push(name + ':' + exec.agent.session.id); return 'ok'; } }));
    for (const name of table.worker.forbidden) {
      for (const result of [await call('call_tool', { name, arguments: {} }, child), await call(name, {}, child)]) {
        assert.equal(result.isError, true, name + ' ran for a worker');
        assert.ok(JSON.stringify(result.content).includes(name + " is the director's"), name + ': ' + JSON.stringify(result.content));
      }
      assert.equal((await call('call_tool', { name, arguments: {} }, root)).isError, false, name + ' for the director');
    }
    assert.ok(!ran.some(entry => entry.endsWith(':child')), 'a forbidden tool executed for the worker: ' + ran);
    assert.equal((await call('call_tool', { name: 'glob', arguments: {} }, child)).value, 'ok', 'a deferred, not forbidden, tool stays callable');
    const listedFor = async subject => (await ctx.systemPrompt.assemble({ agent: subject, scope: subject })).tools.map(t => t.name).sort();
    assert.deepEqual(await listedFor(root), [...table.director.presented].sort());
    assert.deepEqual(await listedFor(child), [...table.worker.presented].sort());
  }, table));

  test('the real assembly: a steward root is shown no section of a tool it cannot call; a director root is shown them all', async () => {
    const registered = ['tool:bash', 'tool:jobs', 'mcp:devmcp', 'mcp-resource-servers', 'aitelier:host-own'];
    const sectionsFor = async rootRole => {
      let names;
      await fixture(async ({ctx, agent}) => {
        for (const name of registered) ctx.systemPrompt.section({ name, order: 50, interpolate: false, text: 'section ' + name });
        const root = agent('root');
        names = (await ctx.systemPrompt.assemble({ agent: root, scope: root })).sections.map(s => s.name);
      }, effective, rootRole);
      return names;
    };
    const steward = await sectionsFor('steward'), director = await sectionsFor(undefined);
    // This deployment names the discovery section itself (aitelier-texts.mjs).
    const discovery = discoveryTexts.sectionName;
    for (const name of ['tool:bash', 'tool:jobs', 'mcp:devmcp', 'mcp-resource-servers'])
      assert.ok(!steward.includes(name), name + ' shown to the steward: ' + steward);
    // Step 24.1a: the steward presents search_tools (its own catalog), so it reads the discovery section.
    assert.ok(steward.includes(discovery), 'the discovery section is the steward\'s now');
    assert.ok(steward.includes('aitelier:host-own'), 'the host\'s own section is kept');
    for (const name of [...registered, discovery])
      assert.ok(director.includes(name), name + ' missing for the director: ' + director);
  });

  test('adapter layer: without aitelier-roles the State tools are not the director\'s, so the guard test above depends on it', () => fixture(async ({ctx, agent, call}) => {
    const child = agent('child');
    for (const name of ['mcp__aitelier__state_graph_write', 'mcp__aitelier__record_evidence'])
      ctx.tools.register(defineTool({ name, description: name, parameters: {}, output, async execute() { return 'ok'; } }));
    assert.equal((await call('call_tool', { name: 'mcp__aitelier__record_evidence', arguments: {} }, child)).isError, false);
    assert.equal((await call('mcp__aitelier__state_graph_write', { action: 'verify_node' }, child)).isError, false);
  }, table));

  test('step 24.1a: a steward root searches and calls only its turn catalog; other sessions get no MCP tool', () => {
    let turn = new Set(['mcp__sw__media__web_search']);
    return fixture(async ({ ctx, agent, call }) => {
      const root = agent('root'), child = agent('child');
      for (const name of ['bash', 'mcp__sw__media__web_search', 'mcp__sw__mailme__search_messages', 'mcp__devmcp__shell'])
        ctx.tools.register(defineTool({ name, description: 'tool ' + name + ' search mail web', parameters: {}, output, async execute() { return name; } }));
      const found = (await call('search_tools', { query: 'search', limit: 10 }, root)).value.tools.map(t => t.name);
      assert.deepEqual(found, ['mcp__sw__media__web_search'], 'never bash, another connection, or the director\'s MCP');
      assert.equal((await call('call_tool', { name: 'mcp__sw__media__web_search', arguments: {} }, root)).isError, false);
      assert.equal((await call('mcp__sw__media__web_search', {}, root)).isError, false);
      for (const name of ['mcp__sw__mailme__search_messages', 'bash', 'mcp__devmcp__shell']) {
        assert.equal((await call('call_tool', { name, arguments: {} }, root)).isError, true, name);
        assert.equal((await call(name, {}, root)).isError, true, name);
      }
      // The catalog is the turn's: a group turn without the private connection's tool.
      turn = new Set();
      assert.equal((await call('mcp__sw__media__web_search', {}, root)).isError, true);
      assert.deepEqual((await call('search_tools', { query: 'search' }, root)).value.tools, []);
      turn = new Set(['mcp__sw__media__web_search']);
      // A session that is not the steward's root (or none) never reaches an MCP tool or the transports.
      for (const name of ['mcp__sw__media__web_search', 'search_tools', 'call_tool'])
        assert.equal((await call(name, name === 'search_tools' ? { query: 'x' } : name === 'call_tool' ? { name: 'bash', arguments: {} } : {}, child)).isError, true, name);
      assert.equal((await ctx.tools.execute({ name: 'mcp__sw__media__web_search', arguments: {}, signal: new AbortController().signal, callId: 'no-agent' })).isError, true);
      // The request's tool list is the same whatever the catalog holds.
      const before = (await ctx.systemPrompt.assemble({ scope: root })).tools;
      turn = new Set(['mcp__sw__media__web_search', 'mcp__sw__mailme__search_messages']);
      assert.deepEqual((await ctx.systemPrompt.assemble({ scope: root })).tools, before);
    }, effective, 'steward', { catalog: id => (id === 'root' ? turn : undefined) });
  });

  test('step 24.1a: a steward without a host catalog finds nothing; a director is unchanged', () => fixture(async ({ ctx, agent, call }) => {
    const root = agent('root');
    ctx.tools.register(defineTool({ name: 'mcp__sw__media__web_search', description: 'web search', parameters: {}, output, async execute() { return 'x'; } }));
    assert.deepEqual((await call('search_tools', { query: 'web' }, root)).value.tools, []);
    assert.equal((await call('mcp__sw__media__web_search', {}, root)).isError, true);
  }, effective, 'steward'));
}
