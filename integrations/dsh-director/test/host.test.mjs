/** Loading refuses a host that cannot work, naming every problem (design §9 MF3/MF4). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateHost } from '../lib/host.mjs';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// A real role table with its two prompts, so the rolesFile check has something to accept.
const tableDir = mkdtempSync(join(tmpdir(), 'dsh-director-roles-'));
const role = (route, prompt) => ({ route, reasoning_effort: 'high', compaction: { thresholdRatio: 0.5 }, prompt, presented: ['bash'], forbidden: [] });
writeFileSync(join(tableDir, 'roles.json'), JSON.stringify({ version: 1, director: role('director', 'director.md'), worker: role('worker', 'worker.md') }));
writeFileSync(join(tableDir, 'director.md'), 'director'); writeFileSync(join(tableDir, 'worker.md'), 'worker');

const good = () => ({ version: '1.2.0', capabilities: ['progress'], progress: () => {},
  config: { sessionId: 's', provider: 'p', model: 'm', stateDir: '/tmp/x', dshEntry: '/x/dsh', rolesFile: join(tableDir, 'roles.json'),
    todo: { module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' } },
  context: async () => '', events: async () => ({ stop() {} }) });

test('a complete host loads; any 1.x version is accepted', () => {
  assert.ok(validateHost(good()));
  assert.ok(validateHost({ ...good(), version: '1.0.0' }));
});

test('no host at all is refused, not run empty', () => {
  assert.throws(() => validateHost(undefined), /directorHost missing/);
});

test('every problem is named in one refusal, never degraded to an empty host', () => {
  const host = { ...good(), version: '0.9.0', context: undefined, capabilities: ['progress', 'heldWork', 'telepathy'],
    workState: async () => 'unknown', config: { ...good().config, model: '', todo: { module: 'dsh_todo' } } };
  assert.throws(() => validateHost(host), error => {
    for (const part of ['expected directorHost ^1, host declares 0.9.0', 'missing required op: context', 'missing config: model',
      'missing config: todo.stateDirEnv', 'capability declared but not implemented: heldWork', 'unknown capability: telepathy',
      'implemented but not declared: workState']) assert.ok(error.message.includes(part), `${part} not named in: ${error.message}`);
    return true;
  });
});

test('the effective role table is validated: adapter extras are allowed, anything else is refused', async () => {
  const { validateEffectiveRoles } = await import('../lib/roles.mjs');
  const role = (route, prompt) => ({ route, reasoning_effort: 'high', compaction: { thresholdRatio: 0.5 }, prompt, presented: ['bash'], forbidden: [] });
  const table = { version: 1, director: role('director', 'director.md'), worker: role('worker', 'worker.md') };
  const extended = { ...table, worker: { ...table.worker, allowed_actions: { write_tool: ['report'] }, notes: { write_tool: 'why' } } };
  assert.equal(validateEffectiveRoles(extended), extended);
  assert.throws(() => validateEffectiveRoles({ ...table, worker: { ...table.worker, allowed_actions: { write_tool: 'report' } } }), /allowed_actions/);
  assert.throws(() => validateEffectiveRoles({ ...table, director: { ...table.director, presented: 'bash' } }), /presented/);
  assert.throws(() => validateEffectiveRoles(Promise.resolve(table)), /role|object|missing/);
});

test('a role table that is absent, or whose prompts are not next to it, is refused at load (not an ENOENT at boot)', () => {
  assert.throws(() => validateHost({ ...good(), config: { ...good().config, rolesFile: join(tableDir, 'missing.json') } }),
    /rolesFile .*missing\.json is not a readable role table/);
  const lonely = mkdtempSync(join(tmpdir(), 'dsh-director-roles-'));
  writeFileSync(join(lonely, 'roles.json'), JSON.stringify({ version: 1, director: role('director', 'director.md'), worker: role('worker', 'worker.md') }));
  assert.throws(() => validateHost({ ...good(), config: { ...good().config, rolesFile: join(lonely, 'roles.json') } }),
    /director prompt director\.md is not next to/);
});

test('the generic held-work refusal names no backend vocabulary', async () => {
  const { dormancyRefusal } = await import('../lib/core.mjs');
  assert.deepEqual(dormancyRefusal({ reason: 'quiet', claims: [{ node_key: 'w1' }] }),
    { refused: 'live_claims', reason: 'you hold work that is not finished', node_keys: ['w1'] });
});
