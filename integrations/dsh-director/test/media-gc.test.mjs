/** Step 24.2b (design-steward 7.8.12a "一个写者一个子目录" 清理, 7.8.18 "导演子目录的清理"):
 *  media-gc.mjs, the daily cleanup the DSH host runs on a stopped director's subdirectory, run as
 *  the host runs it (`node media-gc.mjs <subdirectory>`). No DSH. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const GC = fileURLToPath(new URL('../media-gc.mjs', import.meta.url));
const MiB = 1024 * 1024;
const DAY = 24 * 3600 * 1000;
const root = () => mkdtempSync(join(tmpdir(), 'media-gc-'));
const gc = (...args) => spawnSync(process.execPath, [GC, ...args], { encoding: 'utf8', timeout: 30000 });

/** A writer's subdirectory with one file and index row per entry ({ageMs, size?}). */
function seed(r, userId, entries) {
  mkdirSync(join(r, userId, '2026-10'), { recursive: true, mode: 0o700 });
  const rows = entries.map(({ ageMs, size = 100 }, i) => {
    const file = `${userId}/2026-10/f${i}.pdf`;
    writeFileSync(join(r, file), `%PDF-1.7 ${userId} ${i}`, { mode: 0o600 });
    return { ref: `m_${String(i).padStart(16, 'a')}`, sha256: String(i).repeat(64).slice(0, 64), mime: 'application/pdf', size,
      audience: 'project:p1', source: 'generated', channel: 'director', messageRef: `req-${i}`, index: 0, file,
      at: new Date(Date.now() - ageMs).toISOString() };
  });
  writeFileSync(join(r, userId, 'index.jsonl'), rows.map(row => JSON.stringify(row) + '\n').join(''), { mode: 0o600 });
  return rows;
}
const snapshot = dir => Object.fromEntries(readdirSync(dir, { recursive: true }).sort()
  .map(name => [name, statSync(join(dir, name)).isDirectory() ? 'dir' : readFileSync(join(dir, name), 'utf8')]));
const indexRefs = (r, userId) => readFileSync(join(r, userId, 'index.jsonl'), 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line).ref);

test('media-gc: removes the 31-day-old files and rows of the given subdirectory and touches no sibling', () => {
  const r = root();
  const [old, fresh] = seed(r, 'd-drv1', [{ ageMs: 31 * DAY }, { ageMs: 3600 * 1000 }]);
  seed(r, 'd-drv2', [{ ageMs: 31 * DAY }]);
  seed(r, 'owner', [{ ageMs: 31 * DAY }]);
  const siblings = { 'd-drv2': snapshot(join(r, 'd-drv2')), owner: snapshot(join(r, 'owner')) };
  const run = gc(join(r, 'd-drv1'));
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout), { removed: 1, evicted: 0 });
  assert.ok(!existsSync(join(r, old.file)), 'the old file went');
  assert.ok(existsSync(join(r, fresh.file)), 'the fresh one stays');
  assert.deepEqual(indexRefs(r, 'd-drv1'), [fresh.ref]);
  for (const [name, before] of Object.entries(siblings)) assert.deepEqual(snapshot(join(r, name)), before, name);
});

test('media-gc: a director\'s subdirectory is held to its 1 GiB, the steward\'s to 5 GiB (the writer\'s own limits)', () => {
  const r = root();
  const big = [{ ageMs: 3 * DAY, size: 400 * MiB }, { ageMs: 2 * DAY, size: 400 * MiB }, { ageMs: DAY, size: 400 * MiB }];
  const rows = seed(r, 'd-drv1', big);
  const run = gc(join(r, 'd-drv1'));
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout), { removed: 0, evicted: 1 });
  assert.deepEqual(indexRefs(r, 'd-drv1'), [rows[1].ref, rows[2].ref], 'the oldest went');
  const steward = seed(r, 'owner', big);
  const own = gc(join(r, 'owner'));
  assert.equal(own.status, 0, own.stderr);
  assert.deepEqual(JSON.parse(own.stdout), { removed: 0, evicted: 0 });
  assert.deepEqual(indexRefs(r, 'owner'), steward.map(row => row.ref));
});

test('media-gc: a subdirectory that does not exist has nothing to clean and is not created; a bad call is a usage error', () => {
  const r = root();
  const run = gc(join(r, 'd-gone'));
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout), { removed: 0, evicted: 0 });
  assert.ok(!existsSync(join(r, 'd-gone')));
  assert.equal(gc().status, 2);
  assert.equal(gc('a', 'b').status, 2);
  assert.equal(gc(join(r, '.hidden')).status, 2, 'not a writer\'s subdirectory name');
});
