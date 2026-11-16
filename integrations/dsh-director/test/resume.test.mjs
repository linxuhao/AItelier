import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { lastTurn, resumeCandidates, refusal, recoveryNote, ResumeRecords } from '../lib/resume.mjs';

const turn = (kind, time) => [{ type: 'turn/start', time }, ...(kind ? [{ type: 'turn/end', time: time + 1, data: { reason: { kind } } }] : [])];

test('a cut-off turn reads as interrupted; a turn the worker ended itself does not', () => {
  assert.deepEqual(lastTurn([...turn('completed', 10), ...turn(undefined, 20)]), { lastEventAt: 20, interrupted: true, everTurned: true });
  assert.deepEqual(lastTurn([...turn('completed', 10), ...turn('aborted', 20)]), { lastEventAt: 21, interrupted: true, everTurned: true });
  assert.deepEqual(lastTurn([...turn('aborted', 10), ...turn('completed', 20)]), { lastEventAt: 21, interrupted: false, everTurned: true });
  assert.deepEqual(lastTurn([{ type: 'user/message', time: 5 }]), { lastEventAt: 5, interrupted: false, everTurned: false });
});

test('candidates: unfinished, unsettled, no candidate report, bound to a child that is not the director and not live', () => {
  const plans = [
    { attempt_id: 'ok', child_session: 'w1', remaining: ['write'] },
    { attempt_id: 'done', child_session: 'w2', remaining: [] },
    { attempt_id: 'settled', child_session: 'w3', remaining: ['write'] },
    { attempt_id: 'reported', child_session: 'w4', remaining: ['write'], finish_reported: true },
    { attempt_id: 'rebound', child_session: 'root', remaining: ['write'] },
    { attempt_id: 'live', child_session: 'w5', remaining: ['write'] },
    { attempt_id: 'unbound', remaining: ['write'] },
  ];
  assert.deepEqual(resumeCandidates({ plans, settled: new Set(['settled']), director: 'root', isLive: id => id === 'w5' }).map(p => p.attempt_id), ['ok']);
});

test('a worker is resumed only on every positive signal, and at most twice', () => {
  const row = { mode: 'continuable' }, cut = { lastEventAt: 100, interrupted: true }, base = { row, turn: cut, startedAt: 200, resumes: 0, maxResumes: 2 };
  assert.equal(refusal(base), undefined);
  assert.equal(refusal({ ...base, row: undefined }), 'not_a_direct_child');
  assert.equal(refusal({ ...base, row: { mode: 'one-shot' } }), 'not_continuable');
  assert.equal(refusal({ ...base, turn: undefined }), 'log_unreadable');
  assert.equal(refusal({ ...base, turn: { lastEventAt: 200, interrupted: true } }), 'active_since_this_process_started');
  assert.equal(refusal({ ...base, turn: { lastEventAt: 100, interrupted: false } }), 'not_interrupted');
  assert.equal(refusal({ ...base, resumes: 2 }), 'resume_limit_reached');
  assert.equal(refusal({ ...base, resumes: Infinity }), 'resume_record_unreadable');
});

test('the recovery note names the plan and says: re-measure, a missing worktree is a failure, not a rebuild', () => {
  const note = recoveryNote({ attempt_id: 'att-9', node_key: 'n/9' });
  assert.match(note, /att-9/); assert.match(note, /n\/9/);
  assert.match(note, /worktree and any process you started may be gone/);
  assert.match(note, /re-read git status/); assert.match(note, /re-measure/);
  assert.match(note, /If your worktree is gone, report the plan failed .* do not recreate the worktree/);
  assert.match(note, /Do not repeat items that already passed/);
});

test('resume records count across instances; an unreadable record never resumes blind', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-resume-'));
  try {
    const records = new ResumeRecords(dir);
    assert.equal(await records.count('w/1'), 0);
    await records.add('w/1', { at: 'a' }); await new ResumeRecords(dir).add('w/1', { at: 'b' });
    assert.equal(await new ResumeRecords(dir).count('w/1'), 2);
    await mkdir(join(dir, 'resume'), { recursive: true });
    await writeFile(join(dir, 'resume', 'w2.json'), '{broken');
    assert.equal(await records.count('w2'), Infinity);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
