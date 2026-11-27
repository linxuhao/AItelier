/** Open issues at dormancy (lib/triage.mjs): the rotation that names a few untracked issues per
 *  sleep, its record, and the idle_dormant summary. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pickTriage, TriageRotation, openIssuesSummary, TRIAGE_SHOWN } from '../lib/triage.mjs';

const issue = (n, day) => ({ id: 'iss-' + n, kind: n % 2 ? 'gap' : 'defect', title: 'issue ' + n, created_at: `2026-10-${String(day).padStart(2, '0')}T00:00:00Z` });
const backlog = Array.from({ length: 12 }, (_, i) => issue(i + 1, 28 - i));   // iss-12 is the oldest

test('never named first, oldest first; then the least recently named', () => {
  assert.deepEqual(pickTriage(backlog).map(i => i.id), ['iss-12', 'iss-11', 'iss-10', 'iss-9', 'iss-8']);
  const shown = Object.fromEntries(['iss-12', 'iss-11', 'iss-10', 'iss-9', 'iss-8'].map(id => [id, 1000]));
  assert.deepEqual(pickTriage(backlog, shown).map(i => i.id), ['iss-7', 'iss-6', 'iss-5', 'iss-4', 'iss-3']);
  // All named once: the earliest named come back first (iss-7..iss-12 at 1000, oldest first).
  const all = Object.fromEntries(backlog.map((i, n) => [i.id, n < 6 ? 2000 : 1000]));
  assert.deepEqual(pickTriage(backlog, all, 3).map(i => i.id), ['iss-12', 'iss-11', 'iss-10']);
  assert.equal(pickTriage([], {}).length, 0);
});

test('the rotation walks the whole backlog over successive sleeps, then starts again; issues no longer untracked leave the record', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'triage-'));
  const rotation = new TriageRotation(dir);
  const seen = [];
  for (let sleep = 1; sleep <= 3; sleep++) seen.push((await rotation.next(backlog, sleep * 1000)).map(i => i.id));
  assert.deepEqual(seen[0], ['iss-12', 'iss-11', 'iss-10', 'iss-9', 'iss-8']);
  assert.deepEqual(seen[1], ['iss-7', 'iss-6', 'iss-5', 'iss-4', 'iss-3']);
  assert.deepEqual(seen[2], ['iss-2', 'iss-1', 'iss-12', 'iss-11', 'iss-10'], 'after the whole backlog, the first named come back');
  // The director resolved three: they are no longer untracked and leave the record.
  const left = backlog.filter(i => !['iss-12', 'iss-11', 'iss-10'].includes(i.id));
  await rotation.next(left, 4000);
  const record = JSON.parse(readFileSync(join(dir, 'triage-shown.json'), 'utf8'));
  assert.ok(!['iss-12', 'iss-11', 'iss-10'].some(id => id in record), JSON.stringify(record));
  assert.equal(Object.keys(record).length, left.length);
});

test('an unreadable record starts the rotation over instead of failing the sleep', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'triage-'));
  writeFileSync(join(dir, 'triage-shown.json'), '[not json');
  assert.deepEqual((await new TriageRotation(dir).next(backlog, 1)).map(i => i.id), ['iss-12', 'iss-11', 'iss-10', 'iss-9', 'iss-8']);
});

test('the summary: counts always, the named issues and the note only when something is untracked', () => {
  const now = Date.parse('2026-11-27T00:00:00Z');
  const answer = { total: 14, by_kind: { gap: 8, defect: 6 }, untracked: backlog };
  const summary = openIssuesSummary(answer, pickTriage(backlog), 'NOTE', now);
  assert.equal(summary.total, 14);
  assert.deepEqual(summary.by_kind, { gap: 8, defect: 6 });
  assert.equal(summary.untracked, 12);
  assert.equal(summary.triage.length, TRIAGE_SHOWN);
  assert.deepEqual(summary.triage[0], { id: 'iss-12', kind: 'defect', title: 'issue 12', age_days: 41 });
  assert.equal(summary.note, 'NOTE');
  const none = openIssuesSummary({ total: 2, by_kind: { gap: 2 }, untracked: [] }, [], 'NOTE', now);
  assert.deepEqual(none, { total: 2, by_kind: { gap: 2 }, untracked: 0 });
  // A host that read only part of a long list says so.
  assert.equal(openIssuesSummary({ ...answer, truncated: true }, [], 'NOTE', now).truncated, true);
  assert.equal('truncated' in summary, false);
  // A long title is cut: the result stays small.
  assert.equal(openIssuesSummary({ total: 1, by_kind: {}, untracked: [1] }, [{ id: 'x', kind: 'gap', title: 'y'.repeat(500) }], 'N').triage[0].title.length, 200);
});
