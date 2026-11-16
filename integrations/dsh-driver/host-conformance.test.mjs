/** AItelier's State adapter against the dsh-director host conformance suite: the
 *  release check of design §9 MF5. The protocol-shape assertions run here on a fake
 *  State; context and events need the running director and a backend, and are
 *  proven by the real-DSH scenarios of this suite (smoke, user-message, wake-echo,
 *  dormancy, todo, terminal-plan), which all boot through this host. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { conformance } from '../dsh-director/conformance/index.mjs';
import { aitelierHost, attemptWorkState } from './runtime.mjs';

const env = { AITELIER_DSH_SESSION: 'root', AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver',
  AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_STATE_DIR: '/tmp/aitelier-conformance', AITELIER_DSH_ENTRY: '/x/dsh',
  AITELIER_DRIVER_ID: 'dsh-a' };
const state = {
  '/api/state/query/get_attempt': ({ attempt_id }) => {
    if (attempt_id === 'att-candidate') return { attempt_id, status: 'candidate' };
    if (attempt_id === 'att-running') return { attempt_id, status: 'running' };
    const error = new Error('AItelier get_attempt: HTTP 404'); error.status = 404; throw error;
  },
  '/api/state/query/list_claims': () => ({ result: { claims: [
    { node_key: 'n-mine', status: 'live', driver_id: 'dsh-a' },
    { node_key: 'n-other', status: 'live', driver_id: 'dsh-b' },
    { node_key: 'n-old', status: 'released', driver_id: 'dsh-a' } ] } }),
};
const activity = [];
const request = async (path, body) => {
  if (path.endsWith('/activity')) { activity.push(body); return { ok: true }; }
  if (!state[path]) throw new Error('unexpected State call ' + path);
  return state[path](body);
};

conformance('AItelier State adapter', () => ({ host: aitelierHost(env, { request }),
  seed: { settled: 'att-candidate', active: 'att-running', held: 'n-mine' } }));

test('AItelier adapter: only this driver\'s live claims are held work', async () => {
  assert.deepEqual((await aitelierHost(env, { request }).heldWork()).map(c => c.node_key), ['n-mine']);
});

test('AItelier adapter: attempt statuses map onto work states exactly where attemptSettled draws the line', () => {
  const states = Object.fromEntries(['candidate', 'failed', 'superseded', 'abandoned', 'reserved', 'launching', 'running',
    'paused', 'unknown', 'something-new'].map(status => [status, attemptWorkState({ status })]));
  assert.deepEqual(states, { candidate: 'completed', failed: 'failed', superseded: 'cancelled', abandoned: 'cancelled',
    reserved: 'working', launching: 'working', running: 'working', paused: 'working', unknown: 'working', 'something-new': 'unknown' });
  assert.equal(attemptWorkState(undefined), 'unknown');
  assert.equal(attemptWorkState({ result: { status: 'candidate' } }), 'completed');
});

test('AItelier adapter: a phase report goes to the activity route unchanged', async () => {
  await aitelierHost(env, { request }).progress({ phase: 'dormant', subagents: [], error: '' });
  assert.deepEqual(activity.at(-1), { phase: 'dormant', subagents: [], error: '' });
});
