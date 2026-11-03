import { describe, it, expect } from 'vitest';
import { driverPhaseRank, driverState, driverStateLabelKey, sortDagRows } from '../../lib/driverList';

const row = (project_id: string, created_at: string) => ({project_id, created_at});
const managed = (phase: string, updated_at: string) => ({managed: {phase, updated_at}});

describe('driver ordering shared by the table and the DAG list', () => {
  it('ranks running before waiting before stopped before no driver at all', () => {
    expect(driverPhaseRank('running')).toBeLessThan(driverPhaseRank('waiting'));
    expect(driverPhaseRank('waiting')).toBeLessThan(driverPhaseRank('stopped'));
    expect(driverPhaseRank(undefined)).toBeGreaterThan(driverPhaseRank('stopped'));
  });

  it('sorts a running-but-old DAG above an idle-but-recent one and breaks idle ties by recency', () => {
    const rows = [row('idle-recent', '2020-01-01T00:00:00+00:00'), row('old-running', '2020-01-01T00:00:00+00:00'),
      row('idle-older', '2020-01-01T00:00:00+00:00'), row('no-driver', '2026-09-01T00:00:00+00:00')];
    const table = {projects: {
      'old-running': managed('running', '2020-01-01T00:00:00+00:00'),
      'idle-recent': managed('waiting', '2026-06-02T00:00:00+00:00'),
      'idle-older': managed('waiting', '2026-06-01T00:00:00+00:00'),
      'no-driver': undefined,
    }};
    expect(sortDagRows(rows, table).map(r => r.project_id))
      .toEqual(['old-running', 'idle-recent', 'idle-older', 'no-driver']);
    // The input is not mutated: the catalog page keeps its own order.
    expect(rows.map(r => r.project_id)).toEqual(['idle-recent', 'old-running', 'idle-older', 'no-driver']);
  });

  it('falls back to the catalog created_at and then the id, so the order is total', () => {
    const rows = [row('b', '2026-01-01T00:00:00+00:00'), row('a', '2026-01-01T00:00:00+00:00'), row('c', '2026-02-01T00:00:00+00:00')];
    expect(sortDagRows(rows, null).map(r => r.project_id)).toEqual(['c', 'a', 'b']);
  });

  it('names the user states running / idle / stopped and the transitional ones as they are', () => {
    expect(driverState('running')).toBe('running');
    expect(driverState('waiting')).toBe('idle');
    expect(driverState('stopped')).toBe('stopped');
    expect(driverState(undefined)).toBe('stopped');
    expect(driverState('starting')).toBe('starting');
    expect(driverStateLabelKey('waiting')).toBe('driverStateIdle');
    expect(driverStateLabelKey('nonsense')).toBe('driverStateStopped');
  });
});
