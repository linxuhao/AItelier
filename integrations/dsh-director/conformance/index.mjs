/** The directorHost conformance suite (plugin design §8.3, §9 MF3-MF5).
 *
 *  `conformance(name, make)` registers node:test cases against any host. `make()`
 *  returns { host, seed, api?, reopen? } where `reopen()` (optional) resolves to a new
 *  host over the same durable state - a restart; without it the same host object is
 *  reused - and `seed` names what the host was prepared with:
 *    settled:  a work id whose state is settled (completed | failed | cancelled)
 *    active:   a work id that is still working
 *    held:     the node_key of work this director holds
 *    event():  (only with `api`) makes the host emit one wake event; resolves when written
 *  `api` is the director api the host's events() gets (`notified` collects what
 *  api.notify received and `notifiedIds` the id each event carried); supply it - a case without one is SKIPPED, and the
 *  repository's driver-suite gate counts a skip as a failure.
 *
 *  Every refusal the director makes on load is asserted here too, so a host author
 *  sees the same messages the director prints. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateHost, WORK_STATES, SETTLED_WORK_STATES, HOST_MAJOR } from '../lib/host.mjs';

export function conformance(name, make) {
  test(`${name}: satisfies directorHost ^${HOST_MAJOR} (required ops, config, declared capabilities)`, async () => {
    const { host } = await make();
    assert.equal(validateHost(host), host);
  });
  test(`${name}: the director refuses it, naming the problem, when a required op is removed or the version is foreign`, async () => {
    const { host } = await make();
    for (const op of ['events', 'context']) {
      const broken = { ...host, [op]: undefined };
      assert.throws(() => validateHost(broken), new RegExp(`missing required op: ${op}`));
    }
    assert.throws(() => validateHost({ ...host, version: '2.0.0' }), /expected directorHost \^1, host declares 2\.0\.0/);
    if (host.capabilities?.length) {
      const [first] = host.capabilities;
      assert.throws(() => validateHost({ ...host, [first]: undefined }), new RegExp(`capability declared but not implemented: ${first}`));
    }
  });
  test(`${name}: workState answers a contract state; settled work is settled, live work is not, unknown work fails closed`, async () => {
    const { host, seed } = await make();
    if (!host.workState) return;
    const settled = await host.workState(seed.settled);
    assert.ok(SETTLED_WORK_STATES.has(settled), `settled work answered ${settled}`);
    const active = await host.workState(seed.active);
    assert.ok(WORK_STATES.includes(active) && !SETTLED_WORK_STATES.has(active), `live work answered ${active}`);
    let unknown;
    try { unknown = await host.workState('no-such-work-' + Date.now()); } catch { unknown = 'unknown'; }
    assert.ok(!SETTLED_WORK_STATES.has(unknown), `unknown work must not read as settled: ${unknown}`);
  });
  test(`${name}: heldWork lists the work this director holds, each with its node_key`, async () => {
    const { host, seed } = await make();
    if (!host.heldWork) return;
    const held = await host.heldWork();
    assert.ok(Array.isArray(held));
    assert.ok(held.every(item => typeof item.node_key === 'string' && item.node_key));
    assert.ok(held.some(item => item.node_key === seed.held), `${seed.held} not held: ${JSON.stringify(held)}`);
  });
  test(`${name}: progress accepts a phase report`, async () => {
    const { host } = await make();
    if (!host.progress) return;
    await host.progress({ phase: 'waiting', subagents: [], error: '' });
  });
  test(`${name}: context is text, and one host event reaches the director exactly once`, async t => {
    const { host, seed, api } = await make();
    if (!api) { t.skip('no director api: proven by this host\'s real-DSH scenarios'); return; }
    assert.equal(typeof await host.context('director'), 'string');
    const events = await host.events(api);
    try {
      const before = api.notified.length;
      await seed.event();
      const deadline = Date.now() + 10000;
      while (api.notified.length === before && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
      assert.equal(api.notified.length, before + 1, 'one event, delivered once');
      // Every host event carries an id (directorHost v1.1, plugin step 7): the director
      // recognises a redelivery by it, so it must not depend on anything local.
      const ids = api.notifiedIds ?? [];
      assert.equal(typeof ids.at(-1), 'string', 'the event carried no id');
      assert.ok(ids.at(-1).length > 0);
    } finally {
      // After stop() the delivery ends; `done` must settle (resolve, or reject with the
      // stop's abort - the director ignores it once it disposed) within a bound.
      assert.ok(await stopped(events), 'events.done did not settle after stop()');
    }
  });
  test(`${name}: attention - one question per id: a retry asks nothing new, another text is refused, close is a record and closing twice changes nothing`, async () => {
    const { host } = await make();
    if (!host.attention) return;
    const id = 'conformance-q-' + Date.now();
    const first = await host.attention.ask({ id, text: 'May I restart the host?' });
    assert.ok(!first.refused, JSON.stringify(first));
    const again = await host.attention.ask({ id, text: 'May I restart the host?' });
    assert.ok(!again.refused, 'a retry of the same question was refused: ' + JSON.stringify(again));
    assert.equal((await host.attention.list()).filter(q => q.id === id).length, 1, 'a retry asked a second question');
    const other = await host.attention.ask({ id, text: 'Something else?' });
    assert.equal(other.refused, 'question_id_reused', JSON.stringify(other));
    const closed = await host.attention.close({ id, reason: 'resolved another way' });
    assert.ok(!closed.refused && closed.closed !== false, JSON.stringify(closed));
    assert.equal((await host.attention.list()).filter(q => q.id === id).length, 0, 'a closed question is still open');
    // Two retry shapes, both leaving it closed once: the same request (a host may replay
    // it, answering closed: true again) and another reason (closed: false).
    const same = await host.attention.close({ id, reason: 'resolved another way' });
    assert.ok(!same.refused, 'a same-reason retry was refused: ' + JSON.stringify(same));
    const twice = await host.attention.close({ id, reason: 'again' });
    assert.ok(!twice.refused && twice.closed === false, 'closing twice: ' + JSON.stringify(twice));
    assert.equal((await host.attention.list()).filter(q => q.id === id).length, 0, 'a retry reopened it');
  });
  test(`${name}: an event whose delivery failed is delivered again, under the same id`, async t => {
    const { host, seed, api, reopen } = await make();
    if (!api) { t.skip('no director api: proven by this host\'s real-DSH scenarios'); return; }
    // The director crashed before it recorded the event: notify threw. The id the host
    // gives the redelivery must be the one it gave the failed delivery, or the director
    // cannot recognise the event and every redelivery is a duplicate the model sees.
    const failedIds = [];
    const failing = { ...api, notify: async (_text, options) => { failedIds.push(options?.id); throw new Error('director stopped before recording this event'); } };
    // A host may fail its events() call itself (an event already waiting) or its later
    // delivery; either way the event must come back.
    let events;
    try {
      events = await host.events(failing);
      Promise.resolve(events.done).catch(() => {});   // its failure is the expected one
      await seed.event();
      const deadline = Date.now() + 10000;
      while (!failedIds.length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
    } catch (error) { if (!failedIds.length) throw error; }
    if (events) assert.ok(await stopped(events), 'events.done did not settle after stop()');
    assert.ok(failedIds.length >= 1, 'the failing delivery was never attempted');
    assert.equal(typeof failedIds[0], 'string', 'the failed delivery carried no id');
    const before = api.notified.length;
    events = await (reopen ? await reopen() : host).events(api);   // the next process
    try {
      const again = Date.now() + 10000;
      while (api.notified.length === before && Date.now() < again) await new Promise(resolve => setTimeout(resolve, 20));
      while (!api.notifiedIds.includes(failedIds[0]) && Date.now() < again) await new Promise(resolve => setTimeout(resolve, 20));
      assert.ok(api.notified.length > before, 'the event whose delivery failed was not delivered again');
      assert.ok(api.notifiedIds.includes(failedIds[0]), `redelivered under another id: ${failedIds[0]} vs ${JSON.stringify(api.notifiedIds)}`);
    } finally { assert.ok(await stopped(events), 'events.done did not settle after stop()'); }
  });
}

/** Stop a host's delivery and wait (bounded) for `done` to settle either way. */
async function stopped(events) {
  events.stop();
  return Promise.race([Promise.resolve(events.done).then(() => true, () => true),
    new Promise(resolve => setTimeout(() => resolve(false), 10000))]);
}
