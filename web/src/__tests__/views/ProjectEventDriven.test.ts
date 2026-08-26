/**
 * The project page refreshes when something HAPPENS, not on a stopwatch.
 *
 * It polled four uncached endpoints every 3 seconds. Measured on the live
 * deployment that was 19.2ms of server CPU per visitor per tick — seven times a
 * dashboard visitor — on a single-core process that is also driving live
 * pipeline runs, which put the ceiling at ~85 concurrent visitors. And it was
 * asking "did anything change?" over a connection that was already being told
 * exactly that: the SSE stream was connected, public, and had no subscribers at
 * all except the checkpoint modal.
 *
 * Caching those reads was the alternative. It is the wrong tool for a LIVE
 * view: it buys throughput by making the page staler. Driving from the stream
 * makes it cheaper AND fresher, so that is what is tested here — the safety-net
 * interval is covered in ProjectRunLive.test.ts, and both guarantees are real:
 * events arrive fast, and the page still moves when no event ever arrives.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';

const mockApi = vi.hoisted(() => ({
  pipelineGraph: vi.fn(), getProject: vi.fn(), getTasks: vi.fn(),
  listRuns: vi.fn(), getRunDetail: vi.fn(), retryProject: vi.fn(),
  patchProject: vi.fn(), getCheckpoint: vi.fn(), approveCheckpoint: vi.fn(),
  rejectCheckpoint: vi.fn(), getTrace: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);
vi.mock('svelte-spa-router', () => ({ push: vi.fn(), default: vi.fn() }));

// Capture what the view subscribes to, so the test can deliver an event the
// same way the SSE client would.
const handlers = vi.hoisted(() => new Map<string, Set<(e: unknown) => void>>());
vi.mock('../../lib/sse', () => ({
  on: (t: string, h: (e: unknown) => void) => {
    if (!handlers.has(t)) handlers.set(t, new Set());
    handlers.get(t)!.add(h);
  },
  off: (t: string, h: (e: unknown) => void) => handlers.get(t)?.delete(h),
  connect: vi.fn(), disconnect: vi.fn(),
}));

function emit(event: Record<string, unknown>) {
  for (const h of handlers.get('*') ?? []) h(event);
}

async function open() {
  authStore.set({ canWrite: true, email: 'x@y', permissionResolved: true });
  connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
  mockApi.getProject.mockResolvedValue({
    project_id: 'p1', name: 'P One', status: 'running',
    created_at: '2026-08-26 10:00:00' });
  mockApi.listRuns.mockResolvedValue({ runs: [] });
  mockApi.getCheckpoint.mockResolvedValue({ checkpoint: null });
  mockApi.getTasks.mockResolvedValue([]);
  mockApi.getTrace.mockResolvedValue({ traces: [], has_more: false });
  const view = render(await import('../../views/Project.svelte'),
    { props: { params: { id: 'p1' } } });
  await view.findByText('P One');
  return view;
}

describe('Project page, event-driven', () => {
  beforeEach(() => { vi.clearAllMocks(); handlers.clear(); });

  it('subscribes to every event type, not a list of them', async () => {
    // A view that enumerates event types goes stale the first time the backend
    // adds one, and the symptom is a page that quietly stops updating — the
    // failure mode this whole change exists to remove.
    await open();
    expect(handlers.get('*')?.size).toBeGreaterThan(0);
  });

  it('refetches when an event for this project arrives', async () => {
    await open();
    const before = mockApi.getProject.mock.calls.length;
    emit({ type: 'step_completed', project_id: 'p1' });
    await waitFor(() =>
      expect(mockApi.getProject.mock.calls.length).toBeGreaterThan(before));
  });

  it('ignores another project\'s events', async () => {
    await open();
    const before = mockApi.getProject.mock.calls.length;
    emit({ type: 'step_completed', project_id: 'someone-else' });
    await new Promise((r) => setTimeout(r, 700));
    expect(mockApi.getProject.mock.calls.length).toBe(before);
  });

  it('coalesces a burst into one refetch', async () => {
    // One step emits several events. Refetching per event would replace a
    // 3-second poll with something worse.
    await open();
    const before = mockApi.getProject.mock.calls.length;
    for (let i = 0; i < 20; i++) emit({ type: 'trace', project_id: 'p1' });
    await waitFor(() =>
      expect(mockApi.getProject.mock.calls.length).toBeGreaterThan(before));
    await new Promise((r) => setTimeout(r, 700));
    expect(mockApi.getProject.mock.calls.length).toBe(before + 1);
  });

  it('unsubscribes on destroy', async () => {
    const view = await open();
    expect(handlers.get('*')?.size).toBe(1);
    view.unmount();
    expect(handlers.get('*')?.size ?? 0).toBe(0);
  });
});
