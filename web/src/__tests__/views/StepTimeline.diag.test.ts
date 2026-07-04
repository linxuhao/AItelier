/**
 * DIAGNOSTIC: reproduce the "blank step name" + trace "stuck loading"
 * field reports with REAL production payload shapes.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';
import tracePage from '../fixtures/trace_page.json';

const mockApi = vi.hoisted(() => ({
  getProject: vi.fn(),
  getTasks: vi.fn(),
  listRuns: vi.fn(),
  getRunDetail: vi.fn(),
  retryProject: vi.fn(),
  refreshPlanning: vi.fn(),
  patchProject: vi.fn(),
  getCheckpoint: vi.fn(),
  approveCheckpoint: vi.fn(),
  rejectCheckpoint: vi.fn(),
  getTrace: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);
vi.mock('svelte-spa-router', () => ({ push: vi.fn(), default: vi.fn() }));

const REAL_RUN_DETAIL = {
  id: 'de0427cd', project_id: 'p1', status: 'completed',
  started_at: '2026-07-01 23:24:55', completed_at: '2026-07-02 09:48:50',
  created_at: '2026-07-01 23:24:55', updated_at: '2026-07-02 09:49:59',
  steps: [
    { id: 1399, step_id: 'git_sync_pre', status: 'completed', retry_count: 0,
      attempt: 1, error: '', created_at: '2026-07-01 23:24:55',
      updated_at: '2026-07-01 23:24:57',
      claimed_at: '2026-07-01 23:24:55', completed_at: '2026-07-01 23:24:57' },
    // created_at is run start (23:24:55); the step actually ran 23:26:00→23:29:21.
    // Duration must reflect claimed→completed (3m 21s), NOT updated-since-run-start.
    { id: 1400, step_id: '1', status: 'completed', retry_count: 0, attempt: 1,
      error: '', created_at: '2026-07-01 23:24:55',
      updated_at: '2026-07-01 23:29:21',
      claimed_at: '2026-07-01 23:26:00', completed_at: '2026-07-01 23:29:21' },
  ],
  step_count: 2, completed_steps: 2, failed_steps: 0,
  cache_stats: { cache_hit_tokens: 100, cache_miss_tokens: 50,
                 hit_ratio: 0.66, total_tokens: 150 },
  cache_stats_by_step: {
    '1': { cache_hit_tokens: 90, cache_miss_tokens: 40, hit_ratio: 0.7,
           total_tokens: 130 },
  },
};

describe('run detail step timeline (real payload)', () => {
  it('renders step labels, durations, and per-step cache stats', async () => {
    authStore.set({ canWrite: true, email: 'x@y', permissionResolved: true });
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
    mockApi.getProject.mockResolvedValue({ project_id: 'p1', name: 'P One',
      status: 'completed', created_at: '2026-07-01 23:24:55' });
    mockApi.listRuns.mockResolvedValue({ runs: [{
      id: 'de0427cd', run_id: 'de0427cd', status: 'completed',
      created_at: '2026-07-01 23:24:55', updated_at: '2026-07-02 09:49:59',
      completed_steps: 2, step_count: 2 }] });
    mockApi.getCheckpoint.mockResolvedValue({ checkpoint: null });
    mockApi.getTasks.mockResolvedValue([]);
    mockApi.getRunDetail.mockResolvedValue(REAL_RUN_DETAIL);

    const { container, findByText } = render(
      await import('../../views/Project.svelte'),
      { props: { params: { id: 'p1' } } });
    await findByText('P One');

    await fireEvent.click(container.querySelector('.run-row')!);
    await waitFor(() => {
      expect(container.querySelector('.step-timeline')).not.toBeNull();
    });

    const labels = Array.from(container.querySelectorAll('.step-label'))
      .map((el) => el.textContent?.trim());
    expect(labels).toEqual(['Git Sync', 'Researcher']);

    // Durations must not be NaN with SQLite timestamps, and must measure the
    // step's own claimed→completed window — not elapsed-since-run-start.
    const durations = Array.from(container.querySelectorAll('.step-duration'))
      .map((el) => el.textContent);
    for (const d of durations) expect(d).not.toContain('NaN');
    expect(durations).toEqual(['2s', '3m 21s']);

    // Per-step cache badge from cache_stats_by_step
    const badges = Array.from(container.querySelectorAll('.step-timeline .cache-inline-badge'))
      .map((el) => el.textContent?.trim());
    expect(badges.some((b) => b?.includes('70% cache'))).toBe(true);
  });
});

describe('Trace view (real payload)', () => {
  it('flips off the loading state and renders entries', async () => {
    mockApi.getTrace.mockResolvedValue(tracePage as Record<string, unknown>);
    const { container, queryByText } = render(
      await import('../../views/Trace.svelte'),
      { props: { params: { id: 'p1', runId: 'de0427cd' } } });

    await waitFor(() => {
      expect(queryByText(/Loading traces/i)).toBeNull();
    });
    expect(container.querySelectorAll('.trace-entry').length).toBeGreaterThan(0);
  });
});
