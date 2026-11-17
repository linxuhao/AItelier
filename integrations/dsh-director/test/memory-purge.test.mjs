/** Plugin step 11 (design-steward 3.6/3.7): purge - build, verify, swap, verify, delete;
 *  writes arriving meanwhile go to the spool; a crash at any point is finished or rolled
 *  back at the next boot from the journal and the directories that exist. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryStore } from '../lib/memory/store.mjs';
import { gitEnv } from '../lib/memory/git.mjs';

const USER = 'owner@example.com';
const SECRET = 'PURGE-ME secret account 4242';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-purge-'));
const open = (root, extra = {}) => MemoryStore.open({ root, userId: USER, ...extra });
const repo = root => join(root, USER);
const gitIn = (dir, ...args) => spawnSync('git', ['-c', 'safe.directory=' + dir, ...args], { cwd: dir, env: gitEnv(dir), maxBuffer: 1 << 28 });
const logLines = root => readdirSync(join(repo(root), 'log')).filter(n => n.endsWith('.jsonl'))
  .flatMap(n => readFileSync(join(repo(root), 'log', n), 'utf8').split('\n').filter(Boolean));
const count = (root, id) => logLines(root).filter(l => JSON.parse(l).id === id).length;
/** A hash of every byte under a directory (names, modes left out): "unchanged" means equal. */
function treeHash(dir) {
  const h = createHash('sha256');
  const walk = d => { for (const n of readdirSync(d).sort()) { const p = join(d, n); const st = statSync(p);
    h.update(relative(dir, p) + '\0'); if (st.isDirectory()) walk(p); else h.update(readFileSync(p)); } };
  walk(dir);
  return h.digest('hex');
}
const leftovers = root => readdirSync(root).filter(n => n.includes('.pre-purge-') || n.includes('.purge-new-'));

async function seeded() {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }, { role: 'steward', text: '好的' }]);
  await store.recordTurn('gone', [{ role: 'owner', text: SECRET }, { role: 'steward', text: 'reply stays' }]);
  const p = await store.profile();
  const v1 = await store.updateProfile('profile v1', p.version, 'one');
  await store.updateProfile('profile v2', v1.version, 'two');
  await store.flush();
  return { root, store };
}

test('purge: the text is in no commit, object, reflog or working file; everything else is byte-for-byte kept', async () => {
  const { root, store } = await seeded();
  const commitsBefore = gitIn(repo(root), 'rev-list', '--all', '--count').stdout.toString().trim();
  const profileHistory = gitIn(repo(root), 'log', '--reverse', '--format=', '-p', '--', 'profile.md').stdout.toString();
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.equal(r.status.purge, undefined, 'the status after a purge says nothing is running');
  const has = buffer => buffer.toString('utf8').includes('secret account 4242');
  assert.ok(!has(gitIn(repo(root), 'log', '-p', '--all').stdout), 'git log -p --all');
  assert.ok(!has(gitIn(repo(root), 'cat-file', '--batch-all-objects', '--batch').stdout), 'git cat-file --batch-all-objects');
  assert.equal(gitIn(repo(root), 'fsck', '--unreachable', '--no-reflogs').stdout.toString().trim(), '');
  for (const p of ['refs/original', 'refs/stash', 'lost-found']) assert.ok(!existsSync(join(repo(root), '.git', p)), p);
  assert.ok(!logLines(root).some(l => l.includes('4242')), 'working tree');
  assert.equal(count(root, 't-keep-0'), 1); assert.equal(count(root, 't-keep-1'), 1);
  assert.equal(count(root, 't-gone-1'), 1, 'the other message of the same turn stays');
  assert.equal(count(root, 't-gone-0'), 0, 'no line of the purged entry is left in the log, reduced or not');
  assert.ok(!gitIn(repo(root), 'log', '-p', '--all', '--format=', '--', 'log').stdout.toString().includes('"id":"t-gone-0"'),
    'no historical version of the log names it either (only forgotten.jsonl keeps {id, at, purged, batch})');
  assert.equal(gitIn(repo(root), 'rev-list', '--all', '--count').stdout.toString().trim(), String(Number(commitsBefore) + 1), 'same history plus the forget commit');
  assert.equal(gitIn(repo(root), 'log', '--reverse', '--format=', '-p', '--', 'profile.md').stdout.toString(), profileHistory, 'every profile version kept');
  const forgotten = readFileSync(join(repo(root), 'forgotten.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
  assert.deepEqual(forgotten.map(r => Object.keys(r).sort().join()), ['at,batch,id,purged'], 'only {id, at, purged, batch} remains');
  assert.deepEqual(leftovers(root), []);
  assert.equal((await store.restore('t-gone-0')).refused, 'purged');
  assert.deepEqual((await store.search('secret account')).grep.hits, []);
  assert.ok(r.not_reached.some(s => /other clone/.test(s)));
  const journal = readFileSync(join(root, USER + '.purge.jsonl'), 'utf8');
  assert.ok(!journal.includes('4242'), 'the journal holds ids and phases only');
  await store.close();
});

test('purge, step 3 verification fails: the old repository is untouched, the new one deleted, the owner told', async () => {
  const { root, store } = await seeded();
  await store.forget(['t-gone-0'], { reason: 'first', by: 'owner' }); // the purge then has nothing left to forget
  const before = treeHash(repo(root));
  store.faults = { verify: async () => ['injected: the filter removed one line too many'] };
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, false);
  assert.deepEqual(r.failures, ['injected: the filter removed one line too many']);
  assert.equal(treeHash(repo(root)), before, 'old repository bytes unchanged');
  assert.deepEqual(leftovers(root), []);
  assert.ok(gitIn(repo(root), 'log', '-p', '--all').stdout.toString().includes('secret account 4242'), 'nothing was purged');
  await store.close();
});

test('purge, step 5 re-check fails after the swap: the old repository comes back', async () => {
  const { root, store } = await seeded();
  await store.forget(['t-gone-0'], { reason: 'first', by: 'owner' });
  const before = treeHash(repo(root));
  let calls = 0;
  store.faults = { verify: async () => (++calls === 2 ? ['injected at the re-check'] : []) };
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, false);
  assert.match(r.reason, /old repository is back/);
  assert.equal(treeHash(repo(root)), before);
  assert.deepEqual(leftovers(root), []);
  await store.close();
});

// Writes injected between every pair of steps, on the success path and on both rollbacks:
// every injected entry ends up exactly once in the repository that remains.
for (const path of ['success', 'verify-fails', 'recheck-fails']) {
  test(`purge with a write between every step (${path}): each write lands once in the remaining repository`, async () => {
    const { root, store } = await seeded();
    let calls = 0;
    const injected = [];
    const write = async name => { injected.push(`t-${name}-0`); const r = await store.recordTurn(name, [{ role: 'owner', text: 'during ' + name }]); assert.equal(r.spooled, true); };
    store.faults = {
      beforeBuild: () => write('w12'), afterVerify: () => write('w34'), afterSwap: () => write('w45'), beforeDelete: () => write('w56'),
      verify: async () => (path === 'verify-fails' && ++calls === 1) || (path === 'recheck-fails' && ++calls === 2) ? ['injected'] : [],
    };
    const r = await store.purge(['t-gone-0']);
    assert.equal(r.purged, path === 'success');
    await store.flush();
    for (const id of injected) assert.equal(count(root, id), 1, id);
    assert.equal(readFileSync(join(root, USER + '.spool.jsonl'), 'utf8'), '', 'spool emptied after the replay');
    assert.deepEqual(leftovers(root), []);
    assert.equal(gitIn(repo(root), 'status', '--porcelain').stdout.toString(), '', 'the replay was committed');
    await store.close();
  });
}

test('the spool takes 5000 entries during a purge and loses none; an unwritable spool fails the write', async () => {
  const { root, store } = await seeded();
  store.faults = { afterVerify: async () => {
    for (let t = 0; t < 50; t++) await store.recordTurn('bulk' + t, Array.from({ length: 100 }, (_, i) => ({ role: 'owner', text: `bulk ${t} ${i}` })));
  } };
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  await store.flush();
  assert.equal(logLines(root).filter(l => JSON.parse(l).id.startsWith('t-bulk')).length, 5000);
  mkdirSync(join(root, USER + '.spool.jsonl.blocker'));
  const { renameSync, rmSync } = await import('node:fs');
  rmSync(join(root, USER + '.spool.jsonl'));
  renameSync(join(root, USER + '.spool.jsonl.blocker'), join(root, USER + '.spool.jsonl')); // a directory: not writable as a file
  let refused;
  store.faults = { afterVerify: async () => { refused = await store.recordTurn('lost', [{ role: 'owner', text: 'x' }]).then(() => 'resolved', e => e.code); } };
  await store.purge(['t-gone-1']).catch(() => {});
  assert.equal(refused, 'EISDIR', 'the write is refused, never reported as remembered');
  await store.close();
});

// SIGKILL at every point, with and without a write in flight; the next boot finishes or
// rolls back from the journal and from which directories exist.
const crashFixture = fileURLToPath(new URL('./fixtures/memory-crash.mjs', import.meta.url));
const points = { beforeBuild: 'rolled_back', afterVerify: 'rolled_back', afterRenameOld: 'rolled_back', betweenRenames: 'rolled_back',
  afterRenameNew: 'completed', afterSwap: 'completed', beforeDelete: 'completed' };
for (const [killAt, outcome] of Object.entries(points)) for (const writeDuring of ['0', '1']) {
  test(`SIGKILL at ${killAt}${writeDuring === '1' ? ' with a write in flight' : ''}: the next boot ends it as ${outcome}`, async () => {
    const root = newRoot();
    const child = spawnSync(process.execPath, [crashFixture, root, USER, killAt, writeDuring], { encoding: 'utf8' });
    assert.equal(child.signal, 'SIGKILL', child.stderr + child.stdout);
    const store = await open(root);
    const journal = readFileSync(join(root, USER + '.purge.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
    assert.equal(journal.at(-1).phase, 'done');
    assert.equal(journal.at(-1).outcome, outcome);
    assert.match(store.statusLine().notices[0], new RegExp(outcome === 'completed' ? '已完成' : '已回退'));
    assert.deepEqual(leftovers(root), []);
    assert.equal(gitIn(repo(root), 'fsck', '--full').status, 0);
    const history = gitIn(repo(root), 'log', '-p', '--all').stdout.toString();
    assert.equal(history.includes('secret account 4242'), outcome === 'rolled_back');
    assert.equal(count(root, 't-keep-0'), 1);
    if (writeDuring === '1') assert.equal(count(root, 't-during-0'), 1, 'the spooled write lands exactly once');
    await store.close();
  });
}
