/**
 * Two dashboard behaviours reported as bugs:
 *
 *  1. Clicking a repo-less RUN jumped straight into its raw trace, skipping the
 *     run page every other project links to.
 *  2. The pipeline catalog listed only generated pipelines, so the built-ins and
 *     the base+addon combos (whose label is inherited from the base, and so are
 *     indistinguishable from it without their addon shown) were invisible.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';

const mockApi = vi.hoisted(() => ({
  listRepos: vi.fn(),
  listAllRuns: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  listPipelines: vi.fn(),
  pipelineStateFile: vi.fn(),
  pipelineGraph: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);

const mockRouter = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('svelte-spa-router', () => ({ push: mockRouter.push, default: vi.fn() }));

import UnifiedDashboard from '../../views/UnifiedDashboard.svelte';

const PIPELINES = [
  { config_name: 'dpe_default_v2', label: 'DPE Pipeline', origin: 'native',
    base: 'dpe_default_v2', addons: [], step_count: 18, state_files: [] },
  { config_name: 'dpe_game', label: 'DPE Pipeline', origin: 'native',
    base: 'dpe_default_v2', addons: ['game_harness'], step_count: 22, state_files: [] },
  { config_name: 'gen_cac40', label: 'Cac40', origin: 'generated',
    base: 'gen_cac40', addons: [], step_count: 6, state_files: [] },
];

function setup(pipelines = PIPELINES, runs: Record<string, unknown>[] = []) {
  mockApi.listRepos.mockResolvedValue([]);   // GET /api/repos returns a bare array
  mockApi.listAllRuns.mockResolvedValue({ runs });
  mockApi.listPipelines.mockResolvedValue({ pipelines });
  mockApi.pipelineGraph.mockResolvedValue({
    config_name: 'dpe_game', label: 'DPE Pipeline', origin: 'native',
    base: 'dpe_default_v2', addons: ['game_harness'], addon_steps: ['gh'],
    begin: 'a',
    steps: [
      { id: 'a', type: 'tool', transitions: [{ to: 'gh' }] },
      { id: 'gh', type: 'tool', from_addon: true, transitions: [] },
    ],
  });
  return render(UnifiedDashboard);
}

describe('UnifiedDashboard pipeline catalog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authStore.set({ canWrite: true, email: 'a@b.c', permissionResolved: true });
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 } as never);
  });

  it('lists built-in pipelines alongside generated ones', async () => {
    const { findByText } = setup();
    expect(await findByText('dpe_default_v2')).toBeTruthy();
    expect(await findByText('gen_cac40')).toBeTruthy();
  });

  it('says which pipelines are built-in and which were generated', async () => {
    const { container } = setup();
    await waitFor(() =>
      expect(container.querySelectorAll('.origin-badge').length).toBe(3));
    expect(container.querySelectorAll('.origin-badge.generated')).toHaveLength(1);
  });

  it('shows the addon a composed pipeline carries', async () => {
    // dpe_game and dpe_default_v2 share the label "DPE Pipeline" -- the addon
    // badge is the ONLY thing that tells them apart in this table.
    const { container, findByText } = setup();
    await waitFor(() =>
      expect(container.querySelectorAll('.addon-badge').length).toBe(1));
    expect((await findByText('game_harness')).textContent).toBe('game_harness');
  });

  it('opens the graph for the clicked pipeline', async () => {
    const { container, findByText } = setup();
    await findByText('dpe_game');
    const row = [...container.querySelectorAll('button.graph-toggle')]
      .find((b) => b.parentElement?.textContent?.includes('dpe_game'));
    await fireEvent.click(row as Element);
    await waitFor(() => expect(mockApi.pipelineGraph).toHaveBeenCalledWith('dpe_game'));
    await waitFor(() => expect(container.querySelector('g.node.addon')).toBeTruthy());
  });

  it('does not fetch a graph for a row nobody opened', async () => {
    const { findByText } = setup();
    await findByText('dpe_game');
    expect(mockApi.pipelineGraph).not.toHaveBeenCalled();
  });

  it('finds a composed pipeline by its addon name', async () => {
    const { container, findByText } = setup();
    await findByText('dpe_game');
    const search = container.querySelector('input[type="search"], input#search, input')!;
    await fireEvent.input(search, { target: { value: 'game_harness' } });
    await waitFor(() =>
      expect(container.querySelectorAll('.addon-badge').length).toBe(1));
    expect(container.textContent).not.toContain('gen_cac40');
  });
});

describe('UnifiedDashboard repo-less run links', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authStore.set({ canWrite: true, email: 'a@b.c', permissionResolved: true });
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 } as never);
  });

  it('links a pipeline run to its run page, not straight to the trace', async () => {
    const { findByText } = setup(PIPELINES, [
      { project_id: 'cac40-1', name: 'cac40 run', repo_less: true,
        config_name: 'gen_cac40', status: 'completed', updated_at: 1 },
    ]);
    const link = await findByText('cac40 run');
    expect(link.getAttribute('href')).toBe('#/projects/cac40-1');
  });

  it('keeps the trace one click away', async () => {
    const { container, findByText } = setup(PIPELINES, [
      { project_id: 'cac40-1', name: 'cac40 run', repo_less: true,
        config_name: 'gen_cac40', status: 'completed', updated_at: 1 },
    ]);
    await findByText('cac40 run');
    const trace = [...container.querySelectorAll('a.repo-btn')]
      .find((a) => a.getAttribute('href')?.endsWith('/trace'));
    expect(trace?.getAttribute('href')).toBe('#/projects/cac40-1/trace');
  });
});
