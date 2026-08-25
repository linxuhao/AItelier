/**
 * Tests for Project.svelte view component.
 *
 * Mocks API modules and Svelte stores to verify rendering logic under
 * various states (loading, loaded, error, write-gated, tab switching).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';
import { projectStore } from '../../stores/project';

// ── Mock API module ─────────────────────────────────────────────────

const mockApi = vi.hoisted(() => ({
  getProject: vi.fn(),
  getTasks: vi.fn(),
  listRuns: vi.fn(),
  getRunDetail: vi.fn(),
  retryProject: vi.fn(),
  patchProject: vi.fn(),
  getCheckpoint: vi.fn(),
  approveCheckpoint: vi.fn(),
  rejectCheckpoint: vi.fn(),
  repoStatus: vi.fn().mockResolvedValue({}),
  repoArchiveUrl: vi.fn().mockReturnValue('#'),
}));

vi.mock('../../lib/api', () => mockApi);

// ── Mock format module (test pure functions directly) ───────────────
// We don't need to mock format — import the real functions for helpers.

// ── Mock svelte-spa-router ──────────────────────────────────────────

const mockPush = vi.fn();
vi.mock('svelte-spa-router', () => ({
  push: (...args: unknown[]) => mockPush(...args),
  // Provide a default export for the Router component used in App.svelte,
  // though Project.svelte only imports `push`.
  default: vi.fn(),
}));

// ── Test fixtures ───────────────────────────────────────────────────

const MOCK_PROJECT = {
  project_id: 'test-project',
  name: 'Test Project',
  brief: 'A test project for verification.',
  status: 'running:t_impl',
  created_at: 1700000000,
  current_step: 't_impl',
  config_name: 'dpe_default_v2',
  priority: 1,
};

const MOCK_RUNS = [
  {
    id: 'run-001',
    run_id: 'run-001',
    status: 'completed',
    created_at: 1700000000,
    updated_at: 1700036000,
    step_count: 5,
    completed_steps: 5,
    failed_steps: 0,
    steps: [
      { step_id: '1', status: 'completed' },
      { step_id: '2', status: 'completed' },
      { step_id: '3', status: 'completed' },
      { step_id: 't_impl', status: 'completed' },
      { step_id: '5', status: 'completed' },
    ],
  },
  {
    id: 'run-002',
    run_id: 'run-002',
    status: 'running:t_impl',
    created_at: 1700040000,
    updated_at: 1700043600,
    step_count: 5,
    completed_steps: 3,
    failed_steps: 0,
    steps: [
      { step_id: '1', status: 'completed' },
      { step_id: '2', status: 'completed' },
      { step_id: '3', status: 'completed' },
      { step_id: 't_impl', status: 'running' },
    ],
  },
];

const MOCK_RUN_DETAIL = {
  id: 'run-001',
  run_id: 'run-001',
  status: 'completed',
  created_at: 1700000000,
  config_name: 'dpe_default_v2',
  steps: [
    { step_id: '1', status: 'completed', created_at: 1700000000, updated_at: 1700010000 },
    { step_id: '2', status: 'completed', created_at: 1700010000, updated_at: 1700020000 },
    { step_id: '3', status: 'completed', created_at: 1700020000, updated_at: 1700030000, retry_count: 1 },
    { step_id: 't_impl', status: 'completed', created_at: 1700030000, updated_at: 1700036000, error: 'Retried once' },
    { step_id: '5', status: 'completed', created_at: 1700036000, updated_at: 1700040000 },
  ],
  cache_stats: {
    cache_hit_tokens: 5000,
    cache_miss_tokens: 3000,
    hit_ratio: 0.625,
    total_tokens: 8000,
  },
};

const MOCK_CHECKPOINT = {
  checkpoint: '3_review',
  label: 'Architecture Review',
  step: '3_review',
  project_id: 'test-project',
  step_output: { files: ['output.md'] },
};

// ── Helper to set auth store state ──────────────────────────────────

function setAuth(canWrite: boolean): void {
  authStore.set({ canWrite, email: canWrite ? 'admin@test.com' : null, permissionResolved: true });
}

function setConnected(ok: boolean): void {
  connectionStore.set({ connectionOk: ok, reconnectAttempt: 0 });
}

// ── Tests ───────────────────────────────────────────────────────────

describe('Project.svelte', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setAuth(true);
    setConnected(true);

    // Default mock implementations — successful responses
    mockApi.getProject.mockResolvedValue({ ...MOCK_PROJECT });
    mockApi.listRuns.mockResolvedValue({ runs: MOCK_RUNS.map(r => ({ ...r })) });
    mockApi.getCheckpoint.mockResolvedValue(null);
    mockApi.getTasks.mockResolvedValue([]);
    mockApi.getRunDetail.mockResolvedValue({ ...MOCK_RUN_DETAIL });
  });

  afterEach(() => {
    // Reset store defaults
    authStore.set({ canWrite: false, email: null, permissionResolved: false });
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
    projectStore.set({ currentProjectId: null, projects: [] });
  });

  // ── Loading state ──

  it('shows loading state while fetching data', async () => {
    // Keep promise unresolved during render
    mockApi.getProject.mockReturnValue(new Promise(() => {}));
    mockApi.listRuns.mockReturnValue(new Promise(() => {}));

    const { container } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    // Should show loading indicator — test by checking for aria-busy attribute
    const busyArticle = container.querySelector('article[aria-busy="true"]');
    expect(busyArticle).not.toBeNull();
  });

  // ── Full-page error state ──

  it('shows error state when project fetch fails', async () => {
    mockApi.getProject.mockRejectedValue(new Error('Network error'));

    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await waitFor(() => {
      expect(container.querySelector('.error-state')).not.toBeNull();
    });
    await findByText('Network error');
  });

  // ── 404 redirects to Dashboard ──

  it('redirects to dashboard on 404', async () => {
    const err: any = new Error('Not found');
    err.status = 404;
    mockApi.getProject.mockRejectedValue(err);

    render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'nonexistent' } },
    });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith('#/');
    });
  });

  // ── Renders project info card ──

  it('renders project name and status in info card', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    // Wait for data to load — the project name should appear
    await findByText('Test Project');

    // Status badge should render (compound status with icon)
    const badge = container.querySelector('.status-badge');
    expect(badge).not.toBeNull();

    // Project metadata grid should be present
    const metaLabels = container.querySelectorAll('.meta-label');
    const metaTexts = Array.from(metaLabels).map(el => el.textContent);
    expect(metaTexts.some(t => t === 'Project ID')).toBe(true);
    // textContent is 'Created' — any uppercasing is CSS text-transform,
    // which textContent never sees.
    expect(metaTexts.some(t => t === 'Created')).toBe(true);
  });

  // ── Breadcrumb ──

  it('renders breadcrumb with project ID', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText(/test-project/);

    const breadcrumb = container.querySelector('.breadcrumb');
    expect(breadcrumb).not.toBeNull();
    expect(breadcrumb?.textContent).toContain('Dashboard');
  });

  // ── Run list renders with correct data ──

  it('renders run list with status badges', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    // Wait for data
    await findByText('Test Project');

    // Run table should be present
    const table = container.querySelector('.run-table');
    expect(table).not.toBeNull();

    // Run rows should be rendered
    const rows = container.querySelectorAll('.run-row');
    expect(rows.length).toBe(2);

    // Status badges inside run rows
    const statusBadges = container.querySelectorAll('.run-row .status-badge');
    expect(statusBadges.length).toBe(2);
  });

  // ── Empty state when no runs ──

  it('shows empty state when there are no runs', async () => {
    mockApi.listRuns.mockResolvedValue({ runs: [] });

    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');
    await findByText('No runs yet.');
  });

  // ── Tab switching ──

  it('switches between Runs and Config tabs', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    // Wait for data
    await findByText('Test Project');

    // Find tab bar
    const tabBar = container.querySelector('.tab-bar');
    expect(tabBar).not.toBeNull();

    // Tab buttons
    const tabButtons = tabBar!.querySelectorAll('button');
    expect(tabButtons.length).toBeGreaterThanOrEqual(2);

    // Click Config tab
    const configBtn = Array.from(tabButtons).find(b => b.textContent === 'Configuration');
    expect(configBtn).not.toBeUndefined();
    if (configBtn) {
      await fireEvent.click(configBtn);
    }

    // Config section should now be visible
    await waitFor(() => {
      expect(container.querySelector('.config-section')).not.toBeNull();
    });

    // Click Runs tab
    const runsBtn = Array.from(tabButtons).find(b => b.textContent?.startsWith('Runs'));
    expect(runsBtn).not.toBeUndefined();
    if (runsBtn) {
      await fireEvent.click(runsBtn);
    }

    // Runs content should be visible again
    await waitFor(() => {
      expect(container.querySelector('.run-list-section')).not.toBeNull();
    });
  });

  // ── Config tab write-gating ──

  it('hides Config tab when canWrite is false', async () => {
    setAuth(false);

    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');

    const tabBar = container.querySelector('.tab-bar');
    const tabButtons = tabBar!.querySelectorAll('button');
    const configBtn = Array.from(tabButtons).find(b => b.textContent === 'Configuration');
    expect(configBtn).toBeUndefined();
  });

  // ── Action buttons write-gating ──

  it('shows action buttons when canWrite is true', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');

    // Action bar should have buttons
    const actionBar = container.querySelector('.action-bar');
    expect(actionBar).not.toBeNull();
    expect(actionBar!.querySelectorAll('button').length).toBeGreaterThan(0);
  });

  it('hides action buttons when canWrite is false', async () => {
    setAuth(false);

    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');

    // No action buttons should appear
    const actionBar = container.querySelector('.action-bar');
    expect(actionBar).toBeNull();
  });

  // ── Reconnect overlay ──

  it('shows reconnect overlay when disconnected', async () => {
    setConnected(false);

    const { container } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await waitFor(() => {
      expect(container.querySelector('.reconnect-overlay')).not.toBeNull();
    });
  });

  // ── Checkpoint card ──

  it('shows checkpoint card when checkpoint is pending', async () => {
    mockApi.getCheckpoint.mockResolvedValue({ ...MOCK_CHECKPOINT });

    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    // Substring regex: the h4 renders "Checkpoint Pending: <label>" as one
    // element, and findByText's default matcher requires the FULL text.
    await findByText(/Checkpoint pending/);
    expect(container.querySelector('#checkpoint-card')).not.toBeNull();
  });

  // A rejection in progress belongs to ONE checkpoint. refreshData() replaces
  // `checkpoint` every 3s, and reject mode used to survive that replacement:
  // approve A elsewhere while a rejection for A is half-typed here, and B
  // rendered straight into reject mode carrying A's text, with Approve not
  // drawn at all (it lives in the {:else} branch). One click rejected B with
  // A's words.
  it('drops a half-written rejection when the checkpoint changes underneath it',
     async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockApi.getCheckpoint.mockResolvedValue({ ...MOCK_CHECKPOINT });
      const { container, findByText, findByRole } = render(
        await import('../../views/Project.svelte'),
        { props: { params: { id: 'test-project' } } },
      );
      await findByText(/Checkpoint pending/);

      await fireEvent.click(await findByRole('button', { name: 'Reject' }));
      const box = container.querySelector('#cp-feedback') as HTMLTextAreaElement;
      expect(box).not.toBeNull();
      await fireEvent.input(box, { target: { value: 'redo the schema' } });

      // The run moves on to a DIFFERENT checkpoint while the box is open.
      mockApi.getCheckpoint.mockResolvedValue({
        ...MOCK_CHECKPOINT, checkpoint: '5_review', step: '5_review',
        label: 'Final Review',
      });
      await vi.advanceTimersByTimeAsync(3200);

      // The box is gone with its text, and Approve is available again for the
      // checkpoint now on screen.
      expect(container.querySelector('#cp-feedback')).toBeNull();
      expect(await findByRole('button', { name: 'Approve' })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  // The SAME step coming back after a rejection is a new checkpoint too —
  // rejection_count is what tells them apart.
  it('drops it when the same step returns with a higher rejection_count',
     async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockApi.getCheckpoint.mockResolvedValue(
        { ...MOCK_CHECKPOINT, rejection_count: 0 });
      const { container, findByText, findByRole } = render(
        await import('../../views/Project.svelte'),
        { props: { params: { id: 'test-project' } } },
      );
      await findByText(/Checkpoint pending/);
      await fireEvent.click(await findByRole('button', { name: 'Reject' }));
      await fireEvent.input(
        container.querySelector('#cp-feedback') as HTMLTextAreaElement,
        { target: { value: 'stale' } });

      mockApi.getCheckpoint.mockResolvedValue(
        { ...MOCK_CHECKPOINT, rejection_count: 1 });
      await vi.advanceTimersByTimeAsync(3200);

      expect(container.querySelector('#cp-feedback')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  // ── Navigation to Trace ──

  it('navigates to trace view on View Trace button click', async () => {
    const { findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');
    const traceBtn = await findByText('View trace');
    await fireEvent.click(traceBtn);

    expect(mockPush).toHaveBeenCalledWith('#/projects/test-project/trace');
  });

  // ── Run detail loads on click ──

  it('loads run detail when a run row is clicked', async () => {
    const { container, findByText } = render(await import('../../views/Project.svelte'), {
      props: { params: { id: 'test-project' } },
    });

    await findByText('Test Project');

    // Click the first run row
    const firstRow = container.querySelector('.run-row');
    expect(firstRow).not.toBeNull();
    await fireEvent.click(firstRow!);

    // Run detail panel should appear
    await waitFor(() => {
      expect(container.querySelector('.run-detail-panel')).not.toBeNull();
    });
  });
});
