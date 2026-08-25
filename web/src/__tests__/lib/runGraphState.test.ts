import { describe, it, expect } from 'vitest';
import { runGraphState, rowDuration, type RunStepRow } from '../../lib/runGraphState';

const LOOP_OF = { t_plan: 'task_loop', t_impl: 'task_loop' };

function row(p: Partial<RunStepRow> & { step_id: string }): RunStepRow {
  return { id: p.id ?? 1, status: 'completed', ...p } as RunStepRow;
}

describe('runGraphState', () => {
  it('marks a node with no instances as absent, not pending', () => {
    // "never instantiated" and "queued" are different facts about a run.
    const s = runGraphState(['a', 'b'], [row({ id: 1, step_id: 'a' })]);
    expect(s.byNode.a.status).toBe('completed');
    expect(s.byNode.b.status).toBe('absent');
    expect(s.byNode.b.runs).toBe(0);
  });

  it('reports a failure over a success on the same node', () => {
    const s = runGraphState(['a'], [
      row({ id: 1, step_id: 'a', status: 'completed' }),
      row({ id: 2, step_id: 'a', status: 'failed', error: 'boom' }),
    ]);
    expect(s.byNode.a.status).toBe('failed');
    expect(s.byNode.a.error).toBe('boom');
  });

  it('calls a half-finished fan-out mixed, never completed', () => {
    // Three items done and three still queued is the shape a "completed" badge
    // would misreport as a finished loop.
    const s = runGraphState(['t_impl'], [
      row({ id: 1, step_id: 't_impl', status: 'completed', loop_item: 'a' }),
      row({ id: 2, step_id: 't_impl', status: 'pending', loop_item: 'b' }),
    ], LOOP_OF);
    expect(s.byNode.t_impl.status).toBe('mixed');
  });

  it('collects loop items in claim order', () => {
    const s = runGraphState(['t_plan', 't_impl'], [
      row({ id: 1, step_id: 't_plan', loop_item: 'alpha' }),
      row({ id: 2, step_id: 't_impl', loop_item: 'alpha' }),
      row({ id: 3, step_id: 't_plan', loop_item: 'beta' }),
      row({ id: 4, step_id: 't_impl', loop_item: 'beta' }),
    ], LOOP_OF);
    expect(s.items).toEqual(['alpha', 'beta']);
    expect(s.unattributed).toBe(false);
  });

  it('folds a loop-body node over one item when one is selected', () => {
    const rows = [
      row({ id: 1, step_id: 't_impl', status: 'completed', loop_item: 'alpha' }),
      row({ id: 2, step_id: 't_impl', status: 'failed', loop_item: 'beta', error: 'x' }),
    ];
    expect(runGraphState(['t_impl'], rows, LOOP_OF, 'alpha').byNode.t_impl.status)
      .toBe('completed');
    expect(runGraphState(['t_impl'], rows, LOOP_OF, 'beta').byNode.t_impl.status)
      .toBe('failed');
    // no selection = the whole loop, where one failed item makes the node failed
    expect(runGraphState(['t_impl'], rows, LOOP_OF).byNode.t_impl.status)
      .toBe('failed');
  });

  it('does not filter a node outside the loop when an item is selected', () => {
    // git_sync_pre runs once for the whole run; hiding it under an item filter
    // would blank half the graph whenever a task is selected.
    const s = runGraphState(['git_sync_pre', 't_impl'], [
      row({ id: 1, step_id: 'git_sync_pre', status: 'completed' }),
      row({ id: 2, step_id: 't_impl', status: 'completed', loop_item: 'alpha' }),
    ], LOOP_OF, 'alpha');
    expect(s.byNode.git_sync_pre.status).toBe('completed');
  });

  it('drops un-attributed rows from an item view instead of duplicating them', () => {
    const s = runGraphState(['t_impl'], [
      row({ id: 1, step_id: 't_impl', status: 'completed', loop_item: 'alpha' }),
      row({ id: 2, step_id: 't_impl', status: 'completed', loop_item: null }),
    ], LOOP_OF, 'alpha');
    expect(s.byNode.t_impl.runs).toBe(1);
  });

  it('flags a run recorded before loop_item existed', () => {
    // Nine t_impl rows and no items: the honest reading is "not recorded", and
    // the UI must be able to say so rather than showing one nameless item.
    const s = runGraphState(['t_impl'], [
      row({ id: 1, step_id: 't_impl', loop_item: null }),
      row({ id: 2, step_id: 't_impl', loop_item: null }),
    ], LOOP_OF);
    expect(s.unattributed).toBe(true);
    expect(s.items).toEqual([]);
  });

  it('is not flagged unattributed when the graph has no loop at all', () => {
    const s = runGraphState(['a'], [row({ id: 1, step_id: 'a' })], {});
    expect(s.unattributed).toBe(false);
  });

  it('sums retries across instances', () => {
    const s = runGraphState(['a'], [
      row({ id: 1, step_id: 'a', retry_count: 2 }),
      row({ id: 2, step_id: 'a', retry_count: 1 }),
    ]);
    expect(s.byNode.a.retries).toBe(3);
    expect(s.byNode.a.runs).toBe(2);
  });
});

describe('rowDuration', () => {
  it('measures claim to completion', () => {
    expect(rowDuration({
      id: 1, step_id: 'a', status: 'completed',
      claimed_at: '2026-08-25T10:00:00Z', completed_at: '2026-08-25T10:00:30Z',
    })).toBe(30000);
  });

  it('is null while a step is still running', () => {
    expect(rowDuration({
      id: 1, step_id: 'a', status: 'claimed', claimed_at: '2026-08-25T10:00:00Z',
    })).toBeNull();
  });

  it('is null rather than negative on an out-of-order pair', () => {
    expect(rowDuration({
      id: 1, step_id: 'a', status: 'completed',
      claimed_at: '2026-08-25T10:01:00Z', completed_at: '2026-08-25T10:00:00Z',
    })).toBeNull();
  });
});
