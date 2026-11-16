import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { lastTurn, resumeCandidates, refusal, recoveryNote, ResumeRecords } from '../lib/resume.mjs';

const turn = (kind, time) => [{ type: 'turn/start', time }, ...(kind ? [{ type: 'turn/end', time: time + 1, data: { reason: { kind } } }] : [])];

test('a turn cut by the process ending reads as interrupted; a turn the worker or the model ended does not', () => {
  // The kinds measured on the pinned DSH: a self-stop writes `completed`, a model error
  // `error`; a SIGTERM'd host writes none, and DSH's read closes that turn `interrupted`.
  assert.deepEqual(lastTurn([...turn('completed', 10), ...turn(undefined, 20)]), { lastEventAt: 20, interrupted: true });
  assert.deepEqual(lastTurn([...turn('completed', 10), ...turn('interrupted', 20)]), { lastEventAt: 21, interrupted: true });
  for (const kind of ['completed', 'error', 'aborted', 'cancelled'])
    assert.deepEqual(lastTurn([...turn(undefined, 10), ...turn(kind, 20)]), { lastEventAt: 21, interrupted: false }, kind);
  assert.deepEqual(lastTurn([{ type: 'user/message', time: 5 }]), { lastEventAt: 5, interrupted: false });
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
  const row = { mode: 'continuable' }, cut = { lastEventAt: 100, interrupted: true };
  const base = { row, turn: cut, startedAt: 200, record: { attempts: 0, delivered: 0 }, maxResumes: 2 };
  assert.equal(refusal(base), undefined);
  assert.equal(refusal({ ...base, row: undefined }), 'not_a_direct_child');
  assert.equal(refusal({ ...base, row: { mode: 'one-shot' } }), 'not_continuable');
  assert.equal(refusal({ ...base, turn: undefined }), 'log_unreadable');
  assert.equal(refusal({ ...base, turn: { lastEventAt: 200, interrupted: true } }), 'active_since_this_process_started');
  assert.equal(refusal({ ...base, turn: { lastEventAt: 100, interrupted: false } }), 'not_interrupted');
  assert.equal(refusal({ ...base, record: { attempts: 1, delivered: 1 } }), undefined);
  assert.equal(refusal({ ...base, record: { attempts: 2, delivered: 2 } }), 'resume_limit_reached');
  assert.equal(refusal({ ...base, record: { attempts: 2, delivered: 1 } }), 'resume_attempts_exhausted');
  assert.equal(refusal({ ...base, record: { attempts: 2, delivered: 0 } }), 'resume_attempts_exhausted', 'two failed sends: never resumed, and the reason says so');
  assert.equal(refusal({ ...base, record: undefined }), 'resume_record_unreadable');
});

test('the recovery note names the plan and says: re-measure, a missing worktree is a failure, not a rebuild', () => {
  const note = recoveryNote({ attempt_id: 'att-9', node_key: 'n/9' });
  assert.match(note, /att-9/); assert.match(note, /n\/9/);
  assert.match(note, /worktree and any process you started may be gone/);
  assert.match(note, /re-read git status/); assert.match(note, /re-measure/);
  assert.match(note, /If your worktree is gone, report the plan failed .* do not recreate the worktree/);
  assert.match(note, /Do not repeat items that already passed/);
});

test('resume records count attempts and deliveries across instances; an unreadable record never resumes blind', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-resume-'));
  try {
    const records = new ResumeRecords(dir);
    assert.deepEqual(await records.stats('w/1'), { attempts: 0, delivered: 0 });
    await records.add('w/1', { at: 'a' }); await new ResumeRecords(dir).delivered('w/1');
    await new ResumeRecords(dir).add('w/1', { at: 'b' });           // a send that failed: never marked
    assert.deepEqual(await new ResumeRecords(dir).stats('w/1'), { attempts: 2, delivered: 1 });
    await mkdir(join(dir, 'resume'), { recursive: true });
    await writeFile(join(dir, 'resume', 'w2.json'), '{broken');
    assert.equal(await records.stats('w2'), undefined);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
