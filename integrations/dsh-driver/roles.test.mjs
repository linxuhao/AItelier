/** roles.json: the loader's refusals, the adapter's additions (D3), and that the
 *  runtime keeps no second copy of the table. tests/unit/test_dsh_roles.py checks
 *  the Python loader parses the same file identically and refuses the same tables. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadRoles, parseRoles, validateRoles, roleOf } from './roles.mjs';
import { roles as aitelierRoles, workerJudgeRule } from './aitelier-roles.mjs';
import { refusal } from './tool-discovery.mjs';

const text = readFileSync(new URL('./roles.json', import.meta.url), 'utf8');
const table = loadRoles();
const variant = edit => { const copy = JSON.parse(text); edit(copy); return copy; };

test('roles.json loads, and its prompts exist next to it', () => {
  assert.equal(table.version, 1);
  assert.equal(table.director.prompt, 'driver.md');
  assert.equal(table.worker.prompt, 'worker.md');
  for (const role of ['director', 'worker']) assert.ok(readFileSync(new URL('./' + table[role].prompt, import.meta.url), 'utf8').length > 500);
  assert.equal(roleOf('root', 'root'), 'director');
  assert.equal(roleOf('child', 'root'), 'worker');
  assert.equal(roleOf(undefined, 'root'), 'worker');
});

test('the loader refuses unknown keys at every level, and malformed values', () => {
  const refused = [
    [t => { t.extra = 1; }, /unknown key table\.extra/],
    [t => { t.worker.models = []; }, /unknown key worker\.models/],
    [t => { t.director.compaction.retainRatio = 0.1; }, /unknown key director\.compaction\.retainRatio/],
    [t => { delete t.worker.forbidden; }, /missing key worker\.forbidden/],
    [t => { delete t.director.compaction.thresholdRatio; }, /missing key director\.compaction\.thresholdRatio/],
    [t => { t.version = 2; }, /version must be 1/],
    [t => { t.worker.reasoning_effort = 'xhigh'; }, /reasoning_effort/],
    [t => { t.worker.route = ''; }, /route/],
    [t => { t.worker.compaction.thresholdRatio = 1.5; }, /thresholdRatio/],
    [t => { t.director.compaction.retainTokens = -1; }, /retainTokens/],
    [t => { t.worker.prompt = '../driver.md'; }, /prompt/],
    [t => { t.worker.presented.push('bash'); }, /names a tool twice/],
    [t => { t.worker.presented.push('subagent'); }, /both presents and forbids subagent/],
    [t => { t.worker.forbidden.push('not a name'); }, /list of tool names/],
  ];
  for (const [edit, message] of refused) assert.throws(() => validateRoles(variant(edit)), message, String(edit));
  assert.throws(() => parseRoles('[]'), /table must be an object/);
});

test('the adapter adds AItelier\'s State rules to the generic table and nothing else (D3)', () => {
  const effective = aitelierRoles(table);
  assert.deepEqual(effective.director.presented, [...table.director.presented, 'mcp__aitelier__state_graph_write']);
  assert.deepEqual(effective.director.forbidden, []);
  assert.deepEqual(effective.worker.presented, table.worker.presented);
  assert.deepEqual(effective.worker.forbidden.slice(table.worker.forbidden.length),
    ['declare_driver_presence', 'answer_checkpoint', 'record_evidence', 'verify_node', 'report_external_attempt',
      'start_external_attempt', 'claim_node'].map(n => 'mcp__aitelier__' + n));
  assert.deepEqual(effective.worker.allowed_actions, { mcp__aitelier__state_graph_write: ['report_issue'] });
  assert.deepEqual(JSON.parse(text), table, 'the adapter modified the repository table');
  // Generic refusals keyed on the effective table.
  const exec = (name, args = {}) => ({ name, arguments: args });
  assert.equal(refusal(effective.worker, exec('subagent')), "subagent is the director's");
  assert.equal(refusal(effective.worker, exec('mcp__aitelier__record_evidence')), "mcp__aitelier__record_evidence is the director's: " + workerJudgeRule);
  assert.equal(refusal(effective.worker, exec('mcp__aitelier__state_graph_write', { action: 'verify_node' })),
    "mcp__aitelier__state_graph_write(verify_node) is the director's: " + workerJudgeRule);
  assert.equal(refusal(effective.worker, exec('mcp__aitelier__state_graph_write', { action: 'report_issue' })), undefined);
  assert.equal(refusal(effective.worker, exec('glob')), undefined);
  for (const name of [...effective.worker.forbidden, 'mcp__aitelier__state_graph_write']) assert.equal(refusal(effective.director, exec(name)), undefined, name);
});

test('the runtime keeps no second copy of the table: no tool lists, routes, efforts or compaction values', () => {
  const tools = new Set(['director', 'worker'].flatMap(role => [...table[role].presented, ...table[role].forbidden]));
  const quoted = source => [...source.matchAll(/'([A-Za-z0-9_]+)'/g)].map(m => m[1]);
  const discovery = readFileSync(new URL('./tool-discovery.mjs', import.meta.url), 'utf8');
  // It registers search_tools/call_tool itself; every other table name is the table's.
  assert.deepEqual(quoted(discovery).filter(name => tools.has(name) && !['search_tools', 'call_tool'].includes(name)), []);
  assert.doesNotMatch(discovery, /mcp__aitelier__|state_graph_write/, 'State names belong to the adapter');
  const runtime = readFileSync(new URL('./runtime.mjs', import.meta.url), 'utf8');
  for (const list of runtime.matchAll(/\[[^\][]*\]/g)) {
    if (runtime.slice(0, list.index).endsWith('enum: ')) continue; // project_work's action enum, not tools
    const named = quoted(list[0]).filter(name => tools.has(name));
    assert.ok(named.length < 2, 'a tool list in runtime.mjs: ' + list[0].slice(0, 200));
  }
  for (const source of [discovery, runtime]) {
    assert.doesNotMatch(source, /'(max|high|low|off)'/, 'a reasoning effort literal');
    assert.doesNotMatch(source, /thresholdRatio|retainTokens|0\.25/, 'a compaction value');
    assert.doesNotMatch(source, /'(driver|worker)\.md'/, 'a role prompt named outside roles.json');
    assert.doesNotMatch(source, /model: '(driver|worker)'|route\s*=\s*'/, 'a route literal');
  }
});
