import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { installToolDiscovery, searchTools } from './tool-discovery.mjs';

const require = createRequire(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./package.json', import.meta.url));
const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
const { default: SystemPrompt } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'));
const { default: Tools, defineTool } = await import(require.resolve('@deepseek-ai/dsh-tools'));
const { createScope } = await import(require.resolve('@deepseek-ai/dsh-scope'));
const { HarnessError, createUserMessage } = await import(require.resolve('@deepseek-ai/dsh-llm'));
const output = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: 'rendered:' + JSON.stringify(value) }] };

async function fixture(body) {
  const ctx = new Context();
  const prompt = ctx.plugin(SystemPrompt); await prompt.await();
  const tools = ctx.plugin(Tools); await tools.await();
  installToolDiscovery(ctx, { defineTool, HarnessError, rootSession: 'root' });
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
  ctx.tools.register(defineTool({name:'mcp__aitelier__record_evidence',description:'bare name',parameters:{},output,
    async execute(_args, exec){executed.push(['bare', exec.agent.session.id]);return 'ok';}}));
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
  refusedFor(await call('call_tool',{name:'mcp__aitelier__record_evidence',arguments:{}},child));
  assert.equal((await call('call_tool',write('report_issue'),child)).isError,false, 'other writes stay open to workers');
  assert.deepEqual(executed.filter(([, session]) => session === 'child').map(([action]) => action), ['report_issue']);
  assert.equal(executed.filter(([, session]) => session === 'root').length, 5);
}));
