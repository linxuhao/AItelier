/**
 * The open run's detail has to keep MOVING.
 *
 * It used to be fetched exactly once — on the click that opened it — so a
 * reader watching a live run saw a frozen graph: the node drawn as running had
 * often finished minutes earlier, and the trace pane followed it there. The
 * page already polls every 3s for project/runs/checkpoint; the run detail now
 * rides along, and stops once the run is terminal so a finished run is not
 * re-read forever.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';

const mockApi = vi.hoisted(() => ({
  pipelineGraph: vi.fn(),
  getProject: vi.fn(),
  getTasks: vi.fn(),
  listRuns: vi.fn(),
  getRunDetail: vi.fn(),
  retryProject: vi.fn(),
  patchProject: vi.fn(),
  getCheckpoint: vi.fn(),
  approveCheckpoint: vi.fn(),
  rejectCheckpoint: vi.fn(),
  getTrace: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);
vi.mock('svelte-spa-router', () => ({ push: vi.fn(), default: vi.fn() }));

const GRAPH = {
  config_name: 'dpe_default_v2', label: 'DPE', origin: 'native',
  base: 'dpe_default_v2', addons: [], addon_steps: [], begin: 'a', loops: {},
  steps: [
    { id: 'a', type: 'tool', transitions: [{ to: 'b' }] },
    { id: 'b', type: 'agent', transitions: [] },
  ],
};

function detail(status: string, bStatus: string) {
  return {
    id: 'run-1', run_id: 'run-1', project_id: 'p1', status,
    config_name: 'dpe_default_v2', manifest: { labels: {} },
    steps: [
      { id: 11, step_id: 'a', status: 'completed',
        claimed_at: '2026-08-26T10:00:00Z', completed_at: '2026-08-26T10:01:00Z' },
      { id: 12, step_id: 'b', status: bStatus, claimed_at: '2026-08-26T10:01:00Z' },
    ],
  };
}

async function openRun() {
  authStore.set({ canWrite: true, email: 'x@y', permissionResolved: true });
  connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
  mockApi.getProject.mockResolvedValue({
    project_id: 'p1', name: 'P One', status: 'running',
    created_at: '2026-08-26 10:00:00' });
  mockApi.listRuns.mockResolvedValue({ runs: [{
    id: 'run-1', run_id: 'run-1', status: 'running',
    created_at: '2026-08-26 10:00:00', updated_at: '2026-08-26 10:01:00',
    completed_steps: 1, step_count: 2 }] });
  mockApi.getCheckpoint.mockResolvedValue({ checkpoint: null });
  mockApi.getTasks.mockResolvedValue([]);
  mockApi.pipelineGraph.mockResolvedValue(GRAPH);
  mockApi.getTrace.mockResolvedValue({ traces: [], has_more: false });

  const view = render(await import('../../views/Project.svelte'),
    { props: { params: { id: 'p1' } } });
  await view.findByText('P One');
  await fireEvent.click(view.container.querySelector('.run-row')!);
  return view;
}

describe('Project run detail polling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Only the interval is faked, and BEFORE the component mounts: faking after
    // mount leaves the real interval registered, which makes "no second call"
    // pass without the poll ever having been given a chance to fire.
    // setTimeout stays real so waitFor still works.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  });
  afterEach(() => vi.useRealTimers());

  it('re-reads an active run so the graph advances on its own', async () => {
    mockApi.getRunDetail.mockResolvedValue(detail('running', 'claimed'));
    const { container } = await openRun();
    await waitFor(() => expect(container.querySelector('.step-graph svg')).not.toBeNull());
    expect(mockApi.getRunDetail).toHaveBeenCalledTimes(1);

    // The step advances between polls: `b` finishes, and the graph must follow.
    mockApi.getRunDetail.mockResolvedValue(detail('running', 'completed'));
    await waitFor(() => expect(container.querySelector('.node.is-current')).not.toBeNull());

    vi.advanceTimersByTime(3100);

    await waitFor(() => {
      expect(mockApi.getRunDetail.mock.calls.length).toBeGreaterThan(1);
    });
    // Nothing is running any more, so nothing claims to be.
    await waitFor(() => {
      expect(container.querySelector('.node.is-current')).toBeNull();
    });
  });

  it('leaves a finished run alone instead of re-reading it forever', async () => {
    mockApi.getRunDetail.mockResolvedValue(detail('completed', 'completed'));
    const { container } = await openRun();
    await waitFor(() => expect(container.querySelector('.step-graph svg')).not.toBeNull());
    expect(mockApi.getRunDetail).toHaveBeenCalledTimes(1);

    // Three ticks' worth. The other test proves a tick DOES re-read an active
    // run, so this staying at one call is the guard doing its job, not a poll
    // that never ran.
    vi.advanceTimersByTime(9300);
    await waitFor(() => expect(mockApi.getProject.mock.calls.length).toBeGreaterThan(1));

    expect(mockApi.getRunDetail).toHaveBeenCalledTimes(1);
  });
});
