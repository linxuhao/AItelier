import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { capacityLimits, describeDispatch, isDshLimitRefusal, RefusedDispatches } from '../lib/capacity.mjs';

test('capacity is never above the limit DSH runs with, and one slot is always reserved', () => {
  assert.deepEqual(capacityLimits({ hostLimit: 8, dshLimit: 8, reservedSlots: 1 }),
    { capacity: 8, plugin_limit: 8, dsh_limit: 8, reserved: 1, threshold: 7 });
  assert.equal(capacityLimits({ hostLimit: 10, dshLimit: 3 }).capacity, 3, 'host above DSH: DSH wins');
  assert.equal(capacityLimits({ hostLimit: 2, dshLimit: 3 }).capacity, 2, 'host below DSH: the host may ask for less');
  assert.equal(capacityLimits({ dshLimit: 5 }).capacity, 5, 'no host number: DSH\'s');
  assert.deepEqual(capacityLimits({}), { capacity: 8, plugin_limit: null, dsh_limit: null, reserved: 1, threshold: 7 });
  for (const reservedSlots of [0, -2, undefined, 1.5]) assert.equal(capacityLimits({ dshLimit: 3, reservedSlots }).reserved, 1);
  assert.equal(capacityLimits({ dshLimit: 1 }).threshold, 0);
  for (const hostLimit of [10, 8, 4, 3]) for (const dshLimit of [1, 2, 3, 8])
    assert.ok(capacityLimits({ hostLimit, dshLimit }).capacity <= dshLimit);
});

test('a refused dispatch is named by its description and the attempt / node its prompt carries', () => {
  assert.deepEqual(describeDispatch({ description: 'fix login', prompt: 'Work item.\nattempt_id: att-7f3a\nnode_key: auth/login-fix\n...' }),
    { description: 'fix login', attempt_id: 'att-7f3a', node: 'auth/login-fix' });
  assert.deepEqual(describeDispatch({ description: 'x', prompt: '{"attempt_id": "a1", "node_id": "n9"}' }), { description: 'x', attempt_id: 'a1', node: 'n9' });
  assert.deepEqual(describeDispatch({ description: 'free text', prompt: 'no ids here' }), { description: 'free text' });
  assert.deepEqual(describeDispatch(undefined), { description: '' });
  assert.equal(describeDispatch({ description: 'd'.repeat(500) }).description.length, 200);
});

test('DSH\'s own capacity refusal is recognised; the guard\'s refusal and other errors are not', () => {
  assert.ok(isDshLimitRefusal({ isError: true, error: { message: 'subagent limit reached (active child limit: 3); wait for an existing child to finish' } }));
  assert.ok(isDshLimitRefusal({ isError: true, content: [{ type: 'text', text: 'ACTIVATION_LIMIT_REACHED' }] }));
  assert.ok(!isDshLimitRefusal({ isError: false, content: [{ type: 'text', text: 'subagent limit reached' }] }));
  assert.ok(!isDshLimitRefusal({ isError: true, content: [{ type: 'text', text: '{"refused":"at_capacity","reason":"subagent limit reached"}' }] }));
  assert.ok(!isDshLimitRefusal({ isError: true, error: { message: 'model failed' } }));
});

test('refusals persist until removed, and a removal keeps refusals recorded since', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-refused-'));
  try {
    const first = await new RefusedDispatches(dir).load();
    await first.add({ description: 'a', attempt_id: 'att-a' }, 'plugin');
    await first.add({ description: 'b' }, 'dsh');
    const again = await new RefusedDispatches(dir).load();
    assert.deepEqual(again.entries().map(e => [e.description, e.by]), [['a', 'plugin'], ['b', 'dsh']]);
    const taken = again.entries();
    await again.add({ description: 'c' }, 'plugin');
    await again.remove(taken);
    assert.deepEqual((await new RefusedDispatches(dir).load()).entries().map(e => e.description), ['c']);
    assert.equal(new Set([...taken, ...again.entries()].map(e => e.key)).size, 3, 'keys are unique');
    await (await new RefusedDispatches(dir).load()).remove(again.entries());
    assert.equal(JSON.parse(await readFile(join(dir, 'refused-dispatches.json'), 'utf8')).length, 0);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
