/** The outbox and delivered-id log (plugin step 7): a host event is delivered once per
 *  id; a runtime notice is recorded before delivery and replayed after a restart; a
 *  crash between delivery and the record duplicates, it never loses. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Outbox, wakeKey } from '../lib/outbox.mjs';

const dirs = [];
const fresh = async () => { const dir = await mkdtemp(join(tmpdir(), 'dsh-director-outbox-')); dirs.push(dir); return dir; };
const box = async (dir, delivered, deliver = async text => { delivered.push(text); }, log) => new Outbox({ dir, deliver, keep: 4, log }).load();

test('a host event is delivered once per id, also across a restart; an event without id always', async () => {
  const dir = await fresh(), got = [];
  let outbox = await box(dir, got);
  await outbox.deliverOnce('inbox:a', 'A');
  assert.deepEqual(await outbox.deliverOnce('inbox:a', 'A'), { duplicate: true });
  await outbox.deliverOnce(undefined, 'no id'); await outbox.deliverOnce(undefined, 'no id');
  outbox = await box(dir, got);                       // a restart
  assert.deepEqual(await outbox.deliverOnce('inbox:a', 'A'), { duplicate: true });
  assert.deepEqual(got, ['A', 'no id', 'no id']);
});

test('a notice is recorded before delivery: a delivery that fails is replayed by the next process, once', async () => {
  const dir = await fresh(), got = [];
  const failing = await box(dir, got, async () => { throw new Error('crash during delivery'); });
  await assert.rejects(failing.send('watch:7:1', 'JOB DONE'));
  assert.match(await readFile(join(dir, 'outbox.jsonl'), 'utf8'), /JOB DONE/);
  const next = await box(dir, got);
  assert.deepEqual(await next.replay(), { replayed: [wakeKey('watch:7:1', 'JOB DONE')], failed: [], setAside: [] });
  assert.deepEqual(got, ['JOB DONE']);
  const third = await box(dir, got);
  assert.deepEqual((await third.replay()).replayed, []);
  assert.deepEqual(await third.send('watch:7:1', 'JOB DONE'), { duplicate: true });
  assert.deepEqual(got, ['JOB DONE']);
});

test('a crash after delivery but before the record duplicates the notice; it is never lost', async () => {
  const dir = await fresh(), got = [];
  // The state a crash right after delivery leaves: the notice recorded, its id not.
  await writeFile(join(dir, 'outbox.jsonl'), JSON.stringify({ id: 'handback:a:s', text: 'HANDBACK' }) + '\n');
  got.push('HANDBACK');                                // delivered by the process that crashed
  const next = await box(dir, got);
  await next.replay();
  assert.deepEqual(got, ['HANDBACK', 'HANDBACK'], 'duplicated, not lost');
});

test('compaction keeps the newest delivered ids and every undelivered notice', async () => {
  const dir = await fresh(), got = [];
  const outbox = await box(dir, got);
  for (let i = 0; i < 9; i++) await outbox.deliverOnce('e' + i, 'E' + i);
  await outbox.compact();
  const ids = (await readFile(join(dir, 'delivered.jsonl'), 'utf8')).split('\n').filter(Boolean);
  assert.deepEqual(ids, [5, 6, 7, 8].map(i => wakeKey('e' + i, 'E' + i)));
  assert.equal(await readFile(join(dir, 'outbox.jsonl'), 'utf8'), '');
});

test('the same id with other text is a new wake, not a duplicate - for notices and host events, across a restart too', async () => {
  const dir = await fresh(), got = [];
  let outbox = await box(dir, got);
  // One child stopping twice on one plan: the structural id recurs, the payload differs.
  await outbox.send('handback:att-1:child-1', 'HANDBACK done=1');
  assert.deepEqual(await outbox.send('handback:att-1:child-1', 'HANDBACK done=1'), { duplicate: true });
  assert.deepEqual(await outbox.send('handback:att-1:child-1', 'HANDBACK done=2'), { delivered: true });
  await outbox.deliverOnce('state:x', 'EVENT 1');
  assert.deepEqual(await outbox.deliverOnce('state:x', 'EVENT 2'), { delivered: true });
  outbox = await box(dir, got);                       // a restart
  assert.deepEqual(await outbox.send('handback:att-1:child-1', 'HANDBACK done=2'), { duplicate: true });
  assert.deepEqual(await outbox.send('handback:att-1:child-1', 'HANDBACK done=3'), { delivered: true });
  assert.deepEqual(await outbox.deliverOnce('state:x', 'EVENT 2'), { duplicate: true });
  assert.deepEqual(got, ['HANDBACK done=1', 'HANDBACK done=2', 'EVENT 1', 'EVENT 2', 'HANDBACK done=3']);
});

test('one notice that cannot be delivered never stops a boot: the others go, it is kept, and set aside after three boots', async () => {
  const dir = await fresh(), got = [], logs = [];
  await writeFile(join(dir, 'outbox.jsonl'), [{ id: 'bad', text: 'POISON' }, { id: 'good', text: 'FINE' }].map(e => JSON.stringify(e) + '\n').join(''));
  const deliver = async text => { if (text === 'POISON') throw new Error('refused'); got.push(text); };
  let result = await (await box(dir, got, deliver, line => logs.push(line))).replay();
  assert.deepEqual(result, { replayed: ['good'], failed: ['bad'], setAside: [] });
  assert.deepEqual(got, ['FINE']);
  assert.ok(logs.some(line => line.includes('bad') && line.includes('boot 1 of 3')), logs.join('\n'));
  result = await (await box(dir, got, deliver, line => logs.push(line))).replay();
  assert.deepEqual(result, { replayed: [], failed: ['bad'], setAside: [] });
  result = await (await box(dir, got, deliver, line => logs.push(line))).replay();
  assert.deepEqual(result, { replayed: [], failed: [], setAside: ['bad'] });
  assert.ok(logs.some(line => line.includes('bad') && line.includes('set aside')), logs.join('\n'));
  assert.match(await readFile(join(dir, 'outbox-set-aside.jsonl'), 'utf8'), /POISON/);
  assert.deepEqual(await (await box(dir, got, deliver)).replay(), { replayed: [], failed: [], setAside: [] }, 'set aside means not retried');
  assert.deepEqual(got, ['FINE']);
});

test.after(async () => { for (const dir of dirs) await rm(dir, { recursive: true, force: true }); });
