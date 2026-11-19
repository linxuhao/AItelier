/** Plugin step 11 (design-steward 3.6/3.7): purge - build, verify, swap, verify, delete;
 *  writes arriving meanwhile go to the spool; a crash at any point is finished or rolled
 *  back at the next boot from the journal and the directories that exist. */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryStore } from '../lib/memory/store.mjs';
import { profileSection } from '../lib/memory/tools.mjs';
import { gitEnv } from '../lib/memory/git.mjs';
import { fakeEmbedServer } from './fixtures/embed-fake.mjs';

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
  const read = await store.read(['t-gone-0', 't-gone-1'], { withTurn: true });
  assert.deepEqual(read.not_found, ['t-gone-0'], 'a purged id reads as never there');
  assert.equal(read.forgotten, undefined);
  assert.ok(!JSON.stringify(read).includes('4242'), 'memory_read never returns purged text');
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
let embed;
const embedServer = async () => (embed ??= await fakeEmbedServer());
after(async () => { await embed?.close(); });
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
    // Step 21 (3.8.4 item 10): recovered, and again once a vector load has reconciled, the
    // purged id is not in the vector file and every id there is a live entry.
    const vectorIds = () => readFileSync(join(root, USER + '.vectors.jsonl'), 'utf8').split('\n').slice(1).filter(Boolean).map(l => JSON.parse(l).id);
    const live = s => new Set(s.entries.keys());
    assert.ok(!vectorIds().includes('t-gone-0'));
    assert.ok(vectorIds().every(id => live(store).has(id)), vectorIds().join());
    await store.close();
    const loaded = await open(root, { vector: { url: (await embedServer()).url, model: 'bge-m3-q8_0' }, vectorTuning: { backoffMinMs: 20 } });
    await loaded.vector.whenLoaded();
    assert.ok(!vectorIds().includes('t-gone-0'));
    assert.ok(vectorIds().every(id => live(loaded).has(id)), vectorIds().join());
    await loaded.close();
  });
}

// ---------------------------------------------------------------- crash safety of recovery
const journalOf = root => readFileSync(join(root, USER + '.purge.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
/** A root whose purge really died by SIGKILL at that point (the fixture's argv). */
function crashAt(killAt, flags = '0', mode = 'purge') {
  const root = newRoot();
  const child = spawnSync(process.execPath, [crashFixture, root, USER, killAt, flags, mode], { encoding: 'utf8' });
  assert.equal(child.signal, 'SIGKILL', child.stderr + child.stdout);
  return root;
}
const history = root => gitIn(repo(root), 'log', '-p', '--all').stdout.toString();

// c1: the completed branch deletes the only recoverable copy. A kill inside that delete leaves
// a TORN old copy beside the swapped-in one, and the base commit then re-verified against the
// torn copy, failed, and put it back - promoting a mutilated repository over a verified one.
const tornOldCopy = () => {
  const root = crashAt('afterSwap');
  const plan = journalOf(root).at(-1);
  assert.equal(plan.phase, 'renamed_new', 'a real SIGKILL right after the swap');
  // what an interrupted `rmSync(old, {recursive:true})` leaves: the first subtree gone, the
  // .git still there, so git still answers as if the copy were a whole repository.
  rmSync(join(plan.pre_purge_dir, 'log'), { recursive: true, force: true });
  assert.ok(existsSync(join(plan.pre_purge_dir, '.git')), 'the torn copy still looks like a repository');
  assert.equal(gitIn(plan.pre_purge_dir, 'rev-parse', '--verify', 'HEAD').status, 0, 'git still verifies its HEAD');
  return { root, plan };
};

test('c1: a torn old copy left by an interrupted delete is finished off, never promoted', async () => {
  const { root } = tornOldCopy();
  const store = await open(root); // the base commit puts the torn copy back here and loses the entries
  assert.ok(existsSync(join(repo(root), 'log')), 'the swapped-in copy is in place, not the torn old one');
  assert.equal(count(root, 't-keep-0'), 1, 'the entry that survives the purge is still there');
  assert.ok(!logLines(root).some(l => l.includes('4242')), 'the purged text is gone');
  assert.deepEqual(leftovers(root), [], 'the torn copy is finished off, not put back');
  assert.equal(gitIn(repo(root), 'fsck', '--full').status, 0);
  assert.deepEqual((await store.search('secret account')).grep.hits, []);
  const done = journalOf(root).at(-1);
  assert.equal(done.phase, 'done');
  assert.equal(done.outcome, 'completed');
  await store.close();
});

test('c1: the recovery journals its delete of the old copy before it runs, so a second kill is still decidable', async () => {
  const { root } = tornOldCopy();
  const again = spawnSync(process.execPath, [crashFixture, root, USER, 'recoveryBeforeDelete', '0', 'recover'], { encoding: 'utf8' });
  assert.equal(again.signal, 'SIGKILL', again.stderr + again.stdout);
  const killed = journalOf(root).at(-1);
  assert.equal(killed.phase, 'deleting_old', 'the delete is journalled before it runs');
  assert.equal(killed.from_layout, 'repo/old/no-build', 'the row names the layout it decided from');
  const store = await open(root);
  assert.equal(count(root, 't-keep-0'), 1, 'the surviving entry is still there');
  assert.ok(!logLines(root).some(l => l.includes('4242')), 'the purged text is gone');
  assert.deepEqual(leftovers(root), []);
  const done = journalOf(root).at(-1);
  assert.equal(done.phase, 'done');
  assert.equal(done.outcome, 'completed');
  await store.close();
});

// c2: both rollback windows. Each leaves the repository in place with the build beside it, and
// on the base commit the next boot threw memory_unavailable every single time and needed the
// owner to delete the journal. The windows are reproduced from a real SIGKILL at the nearest
// fixture point plus the renames the window itself performs, and (second loop) from kills
// inside the rollback, which now journals its intent before the first rename.
const renames = (plan, n) => { renameSync(plan.dir, plan.new_dir); if (n > 1) renameSync(plan.pre_purge_dir, plan.dir); };
for (const [name, steps] of [['after its first rename', 1], ['after its second rename', 2]]) {
  test(`c2: the in-purge rollback ${name}: three boots decide from the layout, no hand-edited journal`, async () => {
    const root = crashAt('afterSwap');
    const plan = journalOf(root).at(-1);
    assert.equal(plan.phase, 'renamed_new');
    renames(plan, steps); // the exact bytes a kill inside that window leaves
    for (let boot = 1; boot <= 3; boot++) {
      const store = await open(root); // the base commit throws memory_unavailable here, every boot
      if (boot === 1) {
        assert.match(store.statusLine().notices?.[0] ?? '', /已回退/, 'the owner is told what happened');
        assert.equal((await store.restore('t-gone-0')).restored, 't-gone-0', 'the rolled-back entry is recoverable');
        await store.forget(['t-gone-0'], { reason: 'leave the next boots a clean purge state', by: 'owner' });
      }
      assert.equal(count(root, 't-keep-0'), 1, `boot ${boot}: the surviving entry is there`);
      assert.ok(history(root).includes('4242'), `boot ${boot}: the rolled-back purge left the text in history`);
      const last = journalOf(root).at(-1);
      assert.equal(last.phase, 'done', `boot ${boot}: one terminal row`);
      assert.equal(last.outcome, 'rolled_back', `boot ${boot}: the decision was rollback`);
      assert.deepEqual(leftovers(root), []);
      await store.close();
    }
  });
}

for (const killAt of ['beforeRollback', 'duringRollback', 'afterRollback']) {
  test(`c2: SIGKILL at ${killAt} inside the rollback: the next boot decides, three times over`, async () => {
    const root = crashAt(killAt, 'recheck-fails');
    assert.equal(journalOf(root).at(-1).phase, 'rolling_back', 'the rollback journals before its first rename');
    for (let boot = 1; boot <= 3; boot++) {
      const store = await open(root);
      assert.equal(count(root, 't-keep-0'), 1, `boot ${boot}: the surviving entry is there`);
      assert.ok(history(root).includes('4242'), `boot ${boot}: the rolled-back purge left the text in history`);
      const last = journalOf(root).at(-1);
      assert.equal(last.phase, 'done', `boot ${boot}: one terminal row`);
      assert.equal(last.outcome, 'rolled_back', `boot ${boot}: the decision was rollback`);
      assert.deepEqual(leftovers(root), []);
      await store.close();
    }
  });
}

// c3: every destructive step is journalled before it acts. A forget whose records cannot be
// written must not have stripped the log first: that order is what left an id in neither the
// log nor forgotten.jsonl with the text already gone.
test('c3: a forget that cannot record its records first leaves the text in the log', async () => {
  const { root, store } = await seeded();
  rmSync(join(repo(root), 'forgotten.jsonl'));
  mkdirSync(join(repo(root), 'forgotten.jsonl')); // a directory: the records cannot be written
  await assert.rejects(store.purge(['t-gone-0']));
  assert.equal(count(root, 't-gone-0'), 1, 'nothing is stripped before the record of the strip exists');
  assert.ok(logLines(root).some(l => l.includes('4242')), 'the text is untouched');
  await store.close();
});

for (const [killAt, expected] of [['beforeForgetRecords', 'rolled_back'], ['beforeForgetStrip', 'rolled_back'], ['beforeDelete', 'completed']]) {
  test(`c3: SIGKILL at ${killAt}: the purged id ends up in the log or in forgotten, never in neither`, async () => {
    const root = crashAt(killAt);
    const store = await open(root);
    const forgotten = readFileSync(join(repo(root), 'forgotten.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
    const inLog = count(root, 't-gone-0') > 0;
    const recorded = forgotten.some(r => r.id === 't-gone-0');
    assert.ok(inLog || recorded, `t-gone-0 is in the log (${inLog}) or in forgotten.jsonl (${recorded}), not in neither`);
    const last = journalOf(root).at(-1);
    assert.equal(last.phase, 'done');
    assert.equal(last.outcome, expected);
    assert.equal(count(root, 't-keep-0'), 1);
    // Step 21: killed inside step 1, before its vector filter: the recovery filters plan.ids.
    const vectorIds = readFileSync(join(root, USER + '.vectors.jsonl'), 'utf8').split('\n').slice(1).filter(Boolean).map(l => JSON.parse(l).id);
    assert.deepEqual(vectorIds, ['t-keep-0']);
    // a replay of the same turn never brings the purged entry back
    await store.recordTurn('gone', [{ role: 'owner', text: SECRET }, { role: 'steward', text: 'reply stays' }]);
    await store.flush();
    assert.equal(count(root, 't-gone-0'), inLog ? 1 : 0, 'the replay does not resurrect it');
    assert.deepEqual(leftovers(root), []);
    await store.close();
  });
}

// ---- step 11 fixes (claude-dev-mcp), stacked on the purge-durability branch

test('iss-e4eb: a filter that keeps the text and drops only the id is caught by the text check', async () => {
  const { root, store } = await seeded();
  // The reviewer's mutant: the purged line survives without its id, so no id-keyed check sees it.
  store.faults = { filterLine: (line, purged) => {
    try { const row = JSON.parse(line); if (purged.has(row.id)) { delete row.id; return JSON.stringify(row); } } catch {}
    return line;
  } };
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, false, JSON.stringify(r));
  assert.ok(r.failures.some(f => /still holds the text of t-gone-0/.test(f)), JSON.stringify(r.failures));
  assert.deepEqual(leftovers(root), []);
  await store.close();
});

test('iss-e4eb: the same words in a surviving entry are not a finding; the purged one is still gone', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('a', [{ role: 'owner', text: 'the same words twice' }]);
  await store.recordTurn('b', [{ role: 'owner', text: 'the same words twice' }]);
  await store.flush();
  const r = await store.purge(['t-a-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.equal(count(root, 't-a-0'), 0);
  assert.equal(count(root, 't-b-0'), 1);
  assert.deepEqual((await store.search('same words')).grep.hits.map(h => h.id), ['t-b-0']);
  await store.close();
});

// ---- iss-74e7c675: the journal rows a purge writes, named one by one (equality on the row
// itself, never "some phase in a set"), and the rows a kill inside the build -> verify ->
// rename window leaves for the next boot to decide from.

/** The phase of the journal's last row (or, with `fromEnd`, of the row that many back). */
const phaseSeen = (root, fromEnd = 1) => journalOf(root).at(-fromEnd)?.phase;

test('purge: the journal names forget_done, building, verified, renamed_old and rechecked on the row itself, in order', async () => {
  const { root, store } = await seeded();
  // Step 1's vector filter is the only hook between `forget_done` and `building`, and it is
  // called only when there is a vector file: write one, so the row written after step 1 is
  // observed where it is the last row rather than inferred from the next one.
  const v = Buffer.alloc(1024, 7).toString('base64');
  writeFileSync(join(root, USER + '.vectors.jsonl'), '{"model":"bge-m3-q8_0","dims":1024,"quant":"int8","input_chars":1000}\n'
    + ['t-keep-0', 't-gone-0'].map(id => JSON.stringify({ id, v }) + '\n').join(''), { mode: 0o600 });
  const seen = {};
  store.faults = {
    // the identity write: the filter's own lines go out unchanged, so the purge runs on
    vectorFilterWrite: (line, i) => { if (i === 0) seen.forget_done = phaseSeen(root); return line; },
    beforeBuild: () => { seen.building = phaseSeen(root); },
    afterVerify: () => { seen.verified = phaseSeen(root); },
    betweenRenames: () => { seen.renamed_old = phaseSeen(root); },
    afterSwap: () => { seen.renamed_new = phaseSeen(root); },
    beforeDelete: () => { seen.deleting_old = phaseSeen(root); seen.rechecked = phaseSeen(root, 2); },
  };
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.equal(seen.forget_done, 'forget_done', 'the row written once step 1 is committed');
  assert.equal(seen.building, 'building', 'the row written before the build runs');
  assert.equal(seen.verified, 'verified', 'the row written once step 3 passed');
  assert.equal(seen.renamed_old, 'renamed_old', 'the row written between the two renames');
  assert.equal(seen.renamed_new, 'renamed_new', 'the row written once the swap is complete');
  assert.equal(seen.deleting_old, 'deleting_old', 'the row written before the old copy is deleted');
  // `rechecked` has no hook of its own (it is written and the delete journalled in the same
  // breath), so it is named where it sits: the row before `deleting_old`.
  assert.equal(seen.rechecked, 'rechecked', 'the row written once step 5 passed');
  // The same names as one sequence, so a phase the hooks happen not to sit next to is still
  // pinned: every row this purge wrote, exactly, in order.
  assert.deepEqual(journalOf(root).map(row => row.phase),
    ['forgetting', 'forget_done', 'building', 'verified', 'renamed_old', 'renamed_new', 'rechecked', 'deleting_old', 'done']);
  assert.equal(journalOf(root).at(-1).outcome, 'completed');
  await store.close();
});

// A SIGKILL inside building -> verified -> renamed_old: the rows the purge had written are what
// the next boot decides from and names in its own row, and none of them is rewritten or lost.
for (const [killAt, leftPhases, layout, phase] of [
  ['beforeBuild', ['forgetting', 'forget_done', 'building'], 'repo/no-old/no-build', 'building'],
  ['afterVerify', ['forgetting', 'forget_done', 'building', 'verified'], 'repo/no-old/build', 'verified'],
  ['betweenRenames', ['forgetting', 'forget_done', 'building', 'verified', 'renamed_old'], 'no-repo/old/build', 'renamed_old'],
]) {
  test(`a SIGKILL at ${killAt}, inside building -> verified -> renamed_old: the rows it left name ${phase} and the next boot rolls back from them`, async () => {
    const root = crashAt(killAt); // a real purge, killed at that point
    const left = journalOf(root);
    assert.deepEqual(left.map(row => row.phase), leftPhases, 'every row the purge had written, exactly');
    assert.equal(left.at(-1).phase, phase, 'the row the purge died on');
    const purgeId = left.at(-1).purge_id;
    const store = await open(root);
    const rows = journalOf(root);
    assert.deepEqual(rows.slice(0, leftPhases.length).map(row => row.phase), leftPhases, 'recovery adds rows, never rewrites or drops one');
    assert.ok(rows.slice(0, leftPhases.length).every(row => row.purge_id === purgeId), 'and they are all the same purge');
    const done = rows.at(-1);
    assert.equal(done.phase, 'done');
    assert.equal(done.purge_id, purgeId);
    assert.equal(done.outcome, 'rolled_back', 'nothing was swapped in, so the old repository is the one in place');
    assert.equal(done.recovered_from, phase, 'the boot names the row it decided from');
    assert.equal(done.layout, layout, 'and the layout it saw');
    if (phase !== 'building') {
      const decision = rows.find(row => row.phase === 'rolling_back');
      assert.equal(decision.from_layout, layout, 'the rollback is journalled before its first rename');
    }
    assert.match(store.statusLine().notices?.[0] ?? '', /已回退/);
    assert.equal(count(root, 't-keep-0'), 1, 'the surviving entry is there');
    assert.ok(history(root).includes('4242'), 'the repository in place still holds the entry');
    assert.deepEqual(leftovers(root), []);
    await store.close();
  });
}

test('f7: an interrupted purge\'s notice survives restarts and rides in the snapshot until acknowledged', async () => {
  const root = newRoot();
  const child = spawnSync(process.execPath, [crashFixture, root, USER, 'afterVerify', '0'], { encoding: 'utf8' });
  assert.equal(child.signal, 'SIGKILL', child.stderr + child.stdout);
  let store = await open(root); await store.close();
  store = await open(root); // a second boot: nothing left to recover, the notice is still there
  assert.match(store.statusLine().notices?.[0] ?? '', /已回退/);
  assert.match(await profileSection({ store }), /Memory notices - tell your owner:\n- 上次彻底删除/);
  assert.equal(store.statusLine().notices.length, 1, 'the notice is kept once, not once per boot');
  store.acknowledgeNotices();
  assert.equal(store.statusLine().notices, undefined);
  await store.close();
  store = await open(root);
  assert.doesNotMatch(await profileSection({ store }), /Memory notices/);
  await store.close();
});
