/** Plugin step 11 (design-steward 3.6/3.7): the two recovery gaps of issue
 *  iss-677395b6c48d4334, one witness each.
 *
 *  (a) The ordinary-forget crash window. forget() -> #dropFromWorkTree appends the forgotten
 *      record and THEN strips the log lines. A crash in between leaves the id in both places
 *      and only a purge journal is ever re-read at boot, so an ordinary forget was never
 *      finished: the id stayed searchable AND listed by forgottenList() for ever.
 *  (b) The repository + old copy + build layout. #recoverPurge's `dir && pre` branch can reach
 *      #rollbackSwap, whose first rename threw a bare ENOTEMPTY when the build directory was
 *      still there - an undiagnosed Node error on every boot.
 *
 *  The journal, the fault hook and the directory layout are the ones the store really uses:
 *  (a) is injected at `beforeForgetStrip` (the hook memory-purge's crash fixture SIGKILLs at),
 *  (b) is built on disk with the journal row #recoverPurge reads. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { MemoryStore } from '../lib/memory/store.mjs';
import { gitEnv } from '../lib/memory/git.mjs';

const USER = 'owner@example.com';
const SECRET = 'RECOVERY-ME secret account 90210';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memory-recovery-'));
const open = (root, extra = {}) => MemoryStore.open({ root, userId: USER, ...extra });
const repo = root => join(root, USER);
const gitIn = (dir, ...args) => spawnSync('git', ['-c', 'safe.directory=' + dir, ...args], { cwd: dir, env: gitEnv(dir), encoding: 'utf8' });
const logLines = root => readdirSync(join(repo(root), 'log')).filter(n => n.endsWith('.jsonl'))
  .flatMap(n => readFileSync(join(repo(root), 'log', n), 'utf8').split('\n').filter(Boolean));
const count = (root, id) => logLines(root).filter(l => JSON.parse(l).id === id).length;
const journalOf = root => readFileSync(join(root, USER + '.purge.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
const leftovers = root => readdirSync(root).filter(n => n.includes('.pre-purge-') || n.includes('.purge-new-'));
/** A hash of the working tree (`.git` left out: git refreshes its own index, which is not a
 *  change to the repository's content). "Unchanged" means equal. */
function workTreeHash(dir) {
  const h = createHash('sha256');
  const walk = d => { for (const n of readdirSync(d).sort()) { const p = join(d, n); if (n === '.git') continue;
    const st = statSync(p); h.update(relative(dir, p) + '\0'); if (st.isDirectory()) walk(p); else h.update(readFileSync(p)); } };
  walk(dir);
  return h.digest('hex');
}

/** Two turns and a clean repository; `gone` holds the secret. */
async function seeded() {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }]);
  await store.recordTurn('gone', [{ role: 'owner', text: SECRET }, { role: 'steward', text: 'a reply that stays' }]);
  await store.flush();
  return { root, store };
}

// ---- (a) the ordinary-forget crash window

test('(a) an ordinary forget that crashed between its record and its strip is finished at the next open', async () => {
  const { root, store } = await seeded();
  // The window itself: the hook sits inside #dropFromWorkTree, after the forgotten record is
  // durable and before #stripLogLines. A crash here leaves the id in BOTH places and - unlike
  // a purge - writes no journal row, so nothing would ever revisit it.
  store.faults = { beforeForgetStrip: () => { throw new Error('injected: crash between the forgotten record and the strip'); } };
  await assert.rejects(store.forget(['t-gone-0'], { reason: 'owner asked', by: 'owner' }), /injected/);
  assert.match(readFileSync(join(repo(root), 'forgotten.jsonl'), 'utf8'), /"id":"t-gone-0"[^]*"sha256"/, 'the forgotten record is on disk');
  assert.equal(count(root, 't-gone-0'), 1, 'and the log line the crash never got to strip');
  assert.ok(!existsSync(join(root, USER + '.purge.jsonl')), 'an ordinary forget leaves no journal for a boot to read');
  await store.close();

  const events = [];
  const reopened = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  // The invariant: after an open, nothing is searchable and listed as forgotten at once.
  assert.deepEqual((await reopened.search(SECRET)).grep.hits, [], 'the finished forget is out of the log');
  assert.deepEqual((await reopened.search(SECRET)).ranked.hits, [], 'and out of the ranked index');
  assert.equal(count(root, 't-gone-0'), 0, 'the log line was stripped');
  assert.deepEqual(reopened.forgottenList().map(r => r.id), ['t-gone-0'], 'still listed as forgotten, once');
  assert.deepEqual(events.filter(([e]) => e === 'memory/forget-finished').map(([, f]) => f.ids), [['t-gone-0']], 'the boot says which id it finished');
  assert.equal(gitIn(repo(root), 'status', '--porcelain').stdout.toString(), '', 'the repair went into the boot commit');
  assert.equal(gitIn(repo(root), 'log', '-1', '--format=%s').stdout.trim(), 'recover: finished an interrupted forget at boot');
  assert.equal(count(root, 't-keep-0'), 1, 'the entry beside it is untouched');
  // The order that record-before-strip exists for: the text was never destroyed, only unlisted.
  assert.equal((await reopened.restore('t-gone-0')).restored, 't-gone-0', 'history still returns the text');
  assert.equal((await reopened.search(SECRET)).grep.hits[0].id, 't-gone-0');
  await reopened.close();
});

// ---- (b) the repository + old copy + build layout

/** A repository in place, the old copy beside it and the build a rollback failed to delete:
 *  "repo/old/build" with the journal row #recoverPurge reads (a rollback decided before the
 *  crash). A kill inside a recursive delete of the build leaves exactly this shape. */
async function threeDirectoryLayout() {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }]);
  await store.recordTurn('gone', [{ role: 'owner', text: SECRET }]);
  await store.forget(['t-gone-0'], { reason: 'for the purge that rolled back', by: 'owner' });
  await store.flush();
  await store.close();
  const plan = { purge_id: 'purge-1', ids: ['t-gone-0'], dir: repo(root),
    new_dir: join(root, USER + '.purge-new-1'), pre_purge_dir: join(root, USER + '.pre-purge-1') };
  cpSync(plan.dir, plan.pre_purge_dir, { recursive: true }); // the old copy the swap renamed aside
  cpSync(plan.dir, plan.new_dir, { recursive: true });       // the build the delete did not finish
  appendFileSync(join(root, USER + '.purge.jsonl'), JSON.stringify({ ...plan, phase: 'rolling_back', at: new Date().toISOString() }) + '\n');
  assert.deepEqual(leftovers(root).sort(), [USER + '.pre-purge-1', USER + '.purge-new-1']);
  return { root, plan };
}

test('(b) the repo + old + build layout: the build is discarded and the rollback is the same answer on every boot', async () => {
  const { root } = await threeDirectoryLayout();
  // The base commit throws a bare ENOTEMPTY from the rollback's first rename here.
  let store;
  await assert.doesNotReject(async () => { store = await open(root); },
    'a repo + old + build layout must be decided at boot, never thrown at as ENOTEMPTY');
  const first = journalOf(root).at(-1);
  assert.deepEqual([first.phase, first.outcome], ['done', 'rolled_back'], 'the rollback the journal had already decided');
  assert.equal(first.recovered_from, 'rolling_back');
  assert.equal(first.layout, 'repo/old/build', 'the journal names the layout it decided from');
  assert.match(store.statusLine().notices?.[0] ?? '', /已回退/, 'the owner is told what happened');
  assert.deepEqual(leftovers(root), [], 'the build is gone and the old copy is back in place');
  assert.equal(count(root, 't-keep-0'), 1, 'the surviving entry is there');
  assert.equal(gitIn(repo(root), 'fsck', '--full').status, 0);
  const commits = gitIn(repo(root), 'rev-list', '--all', '--count').stdout.toString().trim();
  const tree = workTreeHash(repo(root));
  await store.close();
  // Boots two and three: the same state, the same decision, no second recovery and no throw.
  for (let boot = 2; boot <= 3; boot++) {
    const again = await open(root);
    const last = journalOf(root).at(-1);
    assert.deepEqual([last.phase, last.outcome, last.recovered_from], ['done', 'rolled_back', 'rolling_back'], `boot ${boot}: the decision did not change`);
    assert.equal(journalOf(root).filter(r => r.phase === 'done').length, 1, `boot ${boot}: one terminal row, not one per boot`);
    assert.equal(gitIn(repo(root), 'rev-list', '--all', '--count').stdout.toString().trim(), commits, `boot ${boot}: no commit`);
    assert.equal(workTreeHash(repo(root)), tree, `boot ${boot}: no byte of the working tree changed`);
    assert.equal(count(root, 't-keep-0'), 1, `boot ${boot}: the surviving entry is there`);
    assert.deepEqual(leftovers(root), [], `boot ${boot}: no leftovers`);
    await again.close();
  }
});
