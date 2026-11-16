/** Loading refuses a host that cannot work, naming every problem (design §9 MF3/MF4). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateHost } from '../lib/host.mjs';

const good = () => ({ version: '1.2.0', capabilities: ['progress'], progress: () => {},
  config: { sessionId: 's', provider: 'p', model: 'm', stateDir: '/tmp/x', dshEntry: '/x/dsh', rolesFile: '/x/roles.json',
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
