/**
 * The pane's live behaviour. Two things are load-bearing:
 *   - the first page is the TAIL (newest-first, then reversed) — opening a
 *     mid-run step on its first prompt and paging forward to reach "now" is
 *     the failure mode this replaces;
 *   - live updates APPEND from an `after_seq` cursor, so a step that has been
 *     running for an hour costs one small request per tick, not a re-read.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const mockApi = vi.hoisted(() => ({ getTrace: vi.fn() }));
vi.mock('../../lib/api', () => mockApi);

const entry = (seq: number, event: string) => ({
  seq, category: 'tool_call', event, step_id: 'b',
  created_at: '2026-08-26T15:03:00Z', payload: { params: {} },
});

async function mount(props: Record<string, unknown>) {
  const NodeTrace = (await import('../../views/NodeTrace.svelte')).default;
  return render(NodeTrace, { props });
}

describe('NodeTrace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('opens on the tail and appends new records from the cursor', async () => {
    mockApi.getTrace.mockResolvedValueOnce({
      // Server order is newest-first; the pane reverses it into reading order.
      traces: [entry(9, 'second'), entry(8, 'first')], has_more: true, order: 'desc',
    });
    const { container } = await mount({
      runId: 'run-1', stepInstanceId: 102, live: true,
    });

    await waitFor(() => {
      expect(container.querySelectorAll('.nt-entry').length).toBe(2);
    });
    const events = Array.from(container.querySelectorAll('.nt-event'))
      .map((e) => e.textContent);
    expect(events).toEqual(['first', 'second']); // oldest at the top, chat-style
    expect(mockApi.getTrace.mock.calls[0][1].order).toBe('desc');

    // A tick later, one new record — fetched from seq 9 forward, not re-read.
    mockApi.getTrace.mockResolvedValueOnce({
      traces: [entry(10, 'third')], has_more: false, order: 'asc',
    });
    await vi.advanceTimersByTimeAsync(3000);

    await waitFor(() => {
      expect(container.querySelectorAll('.nt-entry').length).toBe(3);
    });
    const poll = mockApi.getTrace.mock.calls[1][1];
    expect(poll.order).toBe('asc');
    expect(poll.afterSeq).toBe(9);
    expect(poll.stepInstanceId).toBe(102);
  });

  it('stops polling once the step instance has finished', async () => {
    mockApi.getTrace.mockResolvedValue({
      traces: [entry(9, 'done')], has_more: false, order: 'desc',
    });
    await mount({ runId: 'run-1', stepInstanceId: 102, live: false });

    await waitFor(() => expect(mockApi.getTrace).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(9000);
    expect(mockApi.getTrace).toHaveBeenCalledTimes(1);
  });

  it('asks for nothing at all when no node is selected', async () => {
    const { container } = await mount({
      runId: 'run-1', stepInstanceId: null, live: false,
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockApi.getTrace).not.toHaveBeenCalled();
    expect(container.querySelector('.nt-muted')).not.toBeNull();
  });
});
