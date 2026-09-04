/**
 * A project with ONE run still in progress opens that run's graph by itself.
 *
 * Coming to a project to watch a live run always ended in the same click, so
 * the view makes it the default. It stays a default: several live runs (or
 * none) leave the page untouched, and once the reader closes the panel no
 * later refresh re-opens it.
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

function run(id: string, status: string) {
  return { id, run_id: id, status,
    created_at: '2026-09-04 10:00:00', updated_at: '2026-09-04 10:01:00',
    completed_steps: 1, step_count: 2 };
}

function detail(id: string, status: string) {
  return {
    id, run_id: id, project_id: 'p1', status,
    config_name: 'dpe_default_v2', manifest: { labels: {} },
    steps: [
      { id: 11, step_id: 'a', status: 'completed',
        claimed_at: '2026-09-04T10:00:00Z', completed_at: '2026-09-04T10:01:00Z' },
      { id: 12, step_id: 'b', status: 'claimed', claimed_at: '2026-09-04T10:01:00Z' },
    ],
  };
}

async function mount(runs: Record<string, unknown>[]) {
  authStore.set({ canWrite: true, email: 'x@y', permissionResolved: true });
  connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
  mockApi.getProject.mockResolvedValue({
    project_id: 'p1', name: 'P One', status: 'running',
    created_at: '2026-09-04 10:00:00' });
  mockApi.listRuns.mockResolvedValue({ runs });
  mockApi.getCheckpoint.mockResolvedValue({ checkpoint: null });
  mockApi.getTasks.mockResolvedValue([]);
  mockApi.pipelineGraph.mockResolvedValue(GRAPH);
  mockApi.getTrace.mockResolvedValue({ traces: [], has_more: false });
  mockApi.getRunDetail.mockImplementation(
    async (id: string) => detail(id, 'running'));

  const view = render(await import('../../views/Project.svelte'),
    { props: { params: { id: 'p1' } } });
  await view.findByText('P One');
  return view;
}

describe('Project auto-opens the live run graph', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  });
  afterEach(() => vi.useRealTimers());

  it('opens the graph when exactly one run is in progress', async () => {
    const { container } = await mount([run('r-live', 'running'), run('r-old', 'completed')]);
    await waitFor(() => expect(container.querySelector('.step-graph svg')).not.toBeNull());
    expect(mockApi.getRunDetail).toHaveBeenCalledWith('r-live');
  });

  it('opens nothing when every run is finished', async () => {
    const { container } = await mount([run('r-a', 'completed'), run('r-b', 'failed')]);
    // The rows are there, so the view rendered — only the panel is absent.
    await waitFor(() => expect(container.querySelectorAll('.run-row').length).toBe(2));
    expect(container.querySelector('.run-detail-panel')).toBeNull();
    expect(mockApi.getRunDetail).not.toHaveBeenCalled();
  });

  it('opens nothing when two runs are in progress — picking one would be a guess', async () => {
    const { container } = await mount([run('r-1', 'running'), run('r-2', 'checkpoint:plan')]);
    await waitFor(() => expect(container.querySelectorAll('.run-row').length).toBe(2));
    expect(container.querySelector('.run-detail-panel')).toBeNull();
    expect(mockApi.getRunDetail).not.toHaveBeenCalled();
  });

  it('does not re-open the graph a later refresh after the reader closed it', async () => {
    const { container } = await mount([run('r-live', 'running')]);
    await waitFor(() => expect(container.querySelector('.step-graph svg')).not.toBeNull());

    await fireEvent.click(container.querySelector('.run-detail-panel button')!);
    await waitFor(() => expect(container.querySelector('.run-detail-panel')).toBeNull());

    // A safety-net poll tick: the run is still running, and must stay closed.
    vi.advanceTimersByTime(31000);
    await waitFor(() => expect(mockApi.listRuns.mock.calls.length).toBeGreaterThan(1));
    expect(container.querySelector('.run-detail-panel')).toBeNull();
  });
});
