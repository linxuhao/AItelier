/** The State adapter against the dsh-director host conformance suite: the release
 *  check of design §9 MF5. Every case runs here on a fake State (no backend): the
 *  adapter is given an initialised DriverRuntime over that fake, so context() and
 *  events() run without the DSH-scoped install(); events() binds the goal on a stub
 *  goal service, subscribes, and one synthetic State event must reach api.notify
 *  exactly once. The real-DSH scenarios of this suite boot the same host for real. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { conformance } from '../dsh-director/conformance/index.mjs';
import { aitelierHost, attemptWorkState, DriverRuntime } from './runtime.mjs';

const directories = [];
const envFor = directory => ({ AITELIER_DSH_SESSION: 'root', AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver',
  AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_STATE_DIR: directory, AITELIER_DSH_ENTRY: '/x/dsh', AITELIER_DRIVER_ID: 'dsh-a' });

/** A fake State: the reads the adapter makes, and a subscription that parks until
 *  `release()` hands it one event (from another actor, so it is not an own echo). */
function fakeState() {
  const activity = [];
  // The conversation's question blocks, enough of State's rules for attention: a
  // request_key replays only the same payload; a withdrawal is own + unanswered.
  const asked = new Map(), keys = new Map();
  let parked, released = false;
  const routes = {
    '/api/drivers/me': () => ({ driver_id: 'dsh-a' }),
    '/api/dsh-drivers/alpha/context': () => ({ project_id: 'alpha', driver_id: 'dsh-a', event_seq: 1, nodes: [{ name: 'n', status: 'PENDING' }],
      project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'protocol' }),
    '/api/dsh-drivers/alpha/heartbeat': () => ({ ok: true }),
    '/api/state/query/list_director_messages': () => ({ result: { items: [], matched_total: 0, has_more: false } }),
    '/api/state/query/get_attempt': ({ attempt_id }) => {
      if (attempt_id === 'att-candidate') return { attempt_id, status: 'candidate' };
      if (attempt_id === 'att-running') return { attempt_id, status: 'running' };
      const error = new Error('AItelier get_attempt: HTTP 404'); error.status = 404; throw error;
    },
    '/api/state/commands/send_user_message': body => {
      const payload = JSON.stringify(body);
      if (keys.has(body.request_key)) return keys.get(body.request_key).payload === payload ? keys.get(body.request_key).result : { error: 'idempotency_conflict' };
      const result = { message_id: 'm-' + keys.size, project_id: body.project_id };
      keys.set(body.request_key, { payload, result });
      for (const segment of body.segments ?? []) if (segment.kind === 'question')
        asked.set(segment.id, { message_id: result.message_id, segment_id: segment.id, text: segment.text, status: 'open' });
      return result;
    },
    '/api/state/query/list_user_questions': ({ status }) => ({ result: { questions: [...asked.values()].reverse()
      .filter(q => status === 'all' || q.status === status) } }),
    '/api/state/commands/withdraw_user_question': ({ message_id, segment_id }) => {
      const q = asked.get(segment_id);
      if (!q || q.message_id !== message_id) return { error: 'unknown_question' };
      if (q.status === 'withdrawn') return { withdrawn: false, already_withdrawn: true };
      q.status = 'withdrawn';
      return { withdrawn: true };
    },
    '/api/state/query/list_claims': () => ({ result: { claims: [
      { node_key: 'n-mine', status: 'live', driver_id: 'dsh-a' },
      { node_key: 'n-other', status: 'live', driver_id: 'dsh-b' },
      { node_key: 'n-old', status: 'released', driver_id: 'dsh-a' } ] } }),
  };
  const request = async (path, body, signal) => {
    if (path.endsWith('/activity')) { activity.push(body); return { ok: true }; }
    if (path === '/api/state/query/wait_for_state_change') {
      if (released && body.after < 2) return { timed_out: false, next_after: 2, next_inbox_after: 0,
        events: [{ seq: 2, event_type: 'node_changed', payload: { actor: 'driver:dsh-b', node_key: 'n' } }] };
      return new Promise((resolve, reject) => {
        parked = resolve;
        signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true });
      });
    }
    if (!routes[path]) throw new Error('unexpected State call ' + path);
    return routes[path](body);
  };
  const release = () => { released = true; parked?.({ timed_out: true, next_after: 1, next_inbox_after: 0 }); };
  return { request, release, activity };
}

/** The director api the adapter's events() reads, with stub DSH services. */
function directorApi() {
  const notified = [], notifiedIds = [];
  return { notified, notifiedIds, notify: async (text, options = {}) => { notified.push(text); notifiedIds.push(options.id); }, eventMessage: text => text,
    setHostError: async () => {}, clearHostError: async () => {}, isDisposed: () => false,
    agent: { session: { snapshotEvents: () => [] }, steer: () => {} },
    ctx: { logger: { warn() {}, info() {}, error() {} }, on: () => () => {},
      goals: { get: () => undefined, create: (_agent, goal) => ({ ...goal, phase: 'active', activation: 'armed' }) },
      agents: { withoutInitiator: fn => fn() }, sessionPersistence: { flush: async () => {} } } };
}

async function make() {
  const directory = await mkdtemp(join(tmpdir(), 'aitelier-conformance-'));
  directories.push(directory);
  const state = fakeState();
  const runtime = new DriverRuntime({ project: 'alpha', driver: 'dsh-a', directory, request: state.request, retryDelayMs: 10 });
  await runtime.init();
  const host = aitelierHost(envFor(directory), { request: state.request, runtime });
  // A restart: a new runtime over the same State and the same cursor.json.
  const reopen = async () => {
    const next = new DriverRuntime({ project: 'alpha', driver: 'dsh-a', directory, request: state.request, retryDelayMs: 10 });
    await next.init();
    return aitelierHost(envFor(directory), { request: state.request, runtime: next });
  };
  return { host, reopen, api: directorApi(), state, seed: { settled: 'att-candidate', active: 'att-running', held: 'n-mine', event: async () => state.release() } };
}

conformance('State adapter', make);

test('State adapter: the delivered event is the State event, as one runtime-event notice', async () => {
  const { host, api, seed } = await make();
  const events = await host.events(api);
  await seed.event();
  const deadline = Date.now() + 10000;
  while (!api.notified.length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
  events.stop(); await Promise.resolve(events.done).catch(() => {});
  assert.equal(api.notified.length, 1);
  assert.match(api.notified[0], /^AItelier runtime event:\n/);
  assert.match(api.notified[0], /"event_type":"node_changed"/);
});

test('State adapter: only this driver\'s live claims are held work', async () => {
  const { host } = await make();
  assert.deepEqual((await host.heldWork()).map(c => c.node_key), ['n-mine']);
});

test('State adapter: attempt statuses map onto work states exactly where attemptSettled draws the line', () => {
  const states = Object.fromEntries(['candidate', 'failed', 'superseded', 'abandoned', 'reserved', 'launching', 'running',
    'paused', 'unknown', 'something-new'].map(status => [status, attemptWorkState({ status })]));
  assert.deepEqual(states, { candidate: 'completed', failed: 'failed', superseded: 'cancelled', abandoned: 'cancelled',
    reserved: 'working', launching: 'working', running: 'working', paused: 'working', unknown: 'working', 'something-new': 'unknown' });
  assert.equal(attemptWorkState(undefined), 'unknown');
  assert.equal(attemptWorkState({ result: { status: 'candidate' } }), 'completed');
});

test('State adapter: a phase report goes to the activity route unchanged', async () => {
  const { host, state } = await make();
  await host.progress({ phase: 'dormant', subagents: [], error: '' });
  assert.deepEqual(state.activity.at(-1), { phase: 'dormant', subagents: [], error: '' });
});

test.after(async () => { for (const directory of directories) await rm(directory, { recursive: true, force: true }); });

test('State adapter: ask_owner is exactly one send_user_message with one question block, id = question id (the pinned request)', async () => {
  const { readFileSync } = await import('node:fs');
  const fixture = JSON.parse(readFileSync(new URL('./test-fixtures/ask-owner-request.json', import.meta.url), 'utf8'));
  // tests/unit/test_driver_question_withdraw.py stores this same request through State and
  // compares the row's bytes with a direct send_user_message of the same block.
  const sent = [];
  const directory = await mkdtemp(join(tmpdir(), 'aitelier-ask-'));
  directories.push(directory);
  const host = aitelierHost({ ...envFor(directory), AITELIER_DSH_PROJECT: fixture.project }, { request: async (path, body) => { sent.push([path, body]); return { message_id: 'm-1' }; }, runtime: {} });
  assert.deepEqual(await host.attention.ask(fixture.question), { asked: true, question_id: fixture.question.id, message_id: 'm-1' });
  assert.deepEqual(sent, [['/api/state/commands/send_user_message', fixture.request]]);
});
