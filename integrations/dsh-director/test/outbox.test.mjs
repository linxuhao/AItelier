/** The outbox and delivered-id log (plugin step 7): a host event is delivered once per
 *  id; a runtime notice is recorded before delivery and replayed after a restart; a
 *  crash between delivery and the record duplicates, it never loses. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Outbox } from '../lib/outbox.mjs';

const dirs = [];
const fresh = async () => { const dir = await mkdtemp(join(tmpdir(), 'dsh-director-outbox-')); dirs.push(dir); return dir; };
const box = async (dir, delivered, deliver = async text => { delivered.push(text); }) => new Outbox({ dir, deliver, keep: 4 }).load();

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
  assert.deepEqual(await next.replay(), ['watch:7:1']);
  assert.deepEqual(got, ['JOB DONE']);
  const third = await box(dir, got);
  assert.deepEqual(await third.replay(), []);
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
  assert.deepEqual(ids, ['e5', 'e6', 'e7', 'e8']);
  assert.equal(await readFile(join(dir, 'outbox.jsonl'), 'utf8'), '');
});

test.after(async () => { for (const dir of dirs) await rm(dir, { recursive: true, force: true }); });
