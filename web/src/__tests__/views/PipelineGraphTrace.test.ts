/**
 * The run graph's live behaviour: which node is marked as current, which node's
 * trace the pane opens on with NO click, and what happens once the reader picks
 * a node themselves.
 *
 * The trace is requested by step INSTANCE id, never by step_id — a looped step
 * has one instance per item and their records interleave in the run-wide trace,
 * so asserting the instance id is asserting that the pane shows one step's work
 * rather than a blend of several.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';

const mockApi = vi.hoisted(() => ({
  pipelineGraph: vi.fn(),
  getTrace: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);

const GRAPH = {
  begin: 'a',
  steps: [
    { id: 'a', type: 'tool', transitions: [{ to: 'b' }] },
    { id: 'b', type: 'agent', transitions: [{ to: 'c' }] },
    { id: 'c', type: 'agent', transitions: [{ to: null }] },
  ],
  loops: {},
};

// `b` is mid-flight; `a` is done; `c` has not started.
const STEPS = [
  { id: 101, step_id: 'a', status: 'completed', claimed_at: '2026-08-26T15:00:00Z',
    completed_at: '2026-08-26T15:01:00Z' },
  { id: 102, step_id: 'b', status: 'claimed', claimed_at: '2026-08-26T15:02:00Z' },
  { id: 103, step_id: 'c', status: 'pending' },
];

const TRACE = (seq: number, event: string) => ({
  seq, category: 'tool_call', event, step_id: 'b',
  created_at: '2026-08-26T15:03:00Z', payload: { params: { path: 'scenes' } },
});

async function mountGraph() {
  const PipelineGraph = (await import('../../views/PipelineGraph.svelte')).default;
  return render(PipelineGraph, {
    props: { config: 'dpe_game', runSteps: STEPS, runId: 'run-1' },
  });
}

describe('PipelineGraph — current node and its trace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.pipelineGraph.mockResolvedValue(GRAPH);
    mockApi.getTrace.mockResolvedValue({
      traces: [TRACE(9, 'search')], has_more: false, next_seq: 9, order: 'desc',
    });
  });

  it('marks the running node as current and opens its trace with no click', async () => {
    const { container } = await mountGraph();

    await waitFor(() => {
      expect(container.querySelectorAll('.node').length).toBe(3);
    });

    // Exactly one node is the current one, and it is the running step.
    const current = container.querySelectorAll('.node.is-current');
    expect(current.length).toBe(1);
    expect(current[0].textContent).toContain('b');
    // The halo is a ring of its own, so it cannot overdraw the label.
    expect(current[0].querySelector('rect.current-ring')).not.toBeNull();

    // ...and the pane defaulted to it without any interaction.
    await waitFor(() => {
      expect(mockApi.getTrace).toHaveBeenCalled();
    });
    const [runId, opts] = mockApi.getTrace.mock.calls[0];
    expect(runId).toBe('run-1');
    expect(opts.stepInstanceId).toBe(102); // instance of `b`, not the step_id
    expect(opts.order).toBe('desc');       // tail-first: the interesting end
  });

  it('lets the reader take over, and offers a way back to the live step', async () => {
    const { container, getByText, queryByText } = await mountGraph();
    await waitFor(() => expect(mockApi.getTrace).toHaveBeenCalled());

    // While following there is nothing to "follow" — the button would be noise.
    expect(queryByText('Follow current')).toBeNull();

    mockApi.getTrace.mockClear();
    const nodeA = Array.from(container.querySelectorAll('.node'))
      .find((g) => g.textContent?.includes('a')) as SVGGElement;
    await fireEvent.click(nodeA);

    await waitFor(() => expect(mockApi.getTrace).toHaveBeenCalled());
    expect(mockApi.getTrace.mock.calls[0][1].stepInstanceId).toBe(101);
    // `b` is still the live step even though the pane now reads `a`.
    expect(container.querySelectorAll('.node.is-current')[0].textContent).toContain('b');
    expect(getByText('Follow current')).toBeTruthy();

    // Following resumes on request, back to the live step's instance.
    mockApi.getTrace.mockClear();
    await fireEvent.click(getByText('Follow current'));
    await waitFor(() => expect(mockApi.getTrace).toHaveBeenCalled());
    expect(mockApi.getTrace.mock.calls[0][1].stepInstanceId).toBe(102);
  });

  it('shows no current node once the run is over', async () => {
    const PipelineGraph = (await import('../../views/PipelineGraph.svelte')).default;
    const { container } = render(PipelineGraph, {
      props: {
        config: 'dpe_game', runId: 'run-1',
        runSteps: STEPS.map((s) => ({ ...s, status: 'completed' })),
      },
    });
    await waitFor(() => expect(container.querySelectorAll('.node').length).toBe(3));
    expect(container.querySelectorAll('.node.is-current').length).toBe(0);
  });
});

/**
 * `cache_stats.total_tokens` counts only the tokens the cache accounting
 * COVERS, which is 0 for a provider that reports no cache fields at all
 * (Ollama Cloud). The badge used to be gated on `!= null`, so such a step
 * rendered a badge reading "0" — understating a step that had really
 * processed hundreds of thousands of tokens. Absence of accounting is not a
 * measurement of zero: hide the badge instead of printing a false count.
 */
describe('PipelineGraph — cache badge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.pipelineGraph.mockResolvedValue(GRAPH);
    mockApi.getTrace.mockResolvedValue({
      traces: [], has_more: false, next_seq: 0, order: 'desc',
    });
  });

  async function mountWithCache(cacheByStep: Record<string, unknown>) {
    const PipelineGraph = (await import('../../views/PipelineGraph.svelte')).default;
    return render(PipelineGraph, {
      props: { config: 'dpe_game', runSteps: STEPS, runId: 'run-1', cacheByStep },
    });
  }

  it('shows the badge when the cache accounting covered real tokens', async () => {
    const { container } = await mountWithCache({
      b: { cache_hit_tokens: 8000, cache_miss_tokens: 2000, hit_ratio: 0.8, total_tokens: 10000 },
    });
    await waitFor(() => {
      expect(container.querySelector('.cache-inline-badge')).not.toBeNull();
    });
    expect(container.querySelector('.cache-inline-badge')?.textContent).toContain('80% cache');
  });

  it('shows the volume without a ratio when the provider reported no cache accounting', async () => {
    // total_tokens is what was PROCESSED; covered_tokens is what the cache
    // accounting can speak for. A silent provider (Ollama Cloud) leaves the
    // second at 0 and the ratio null — but the step still did the work.
    // Measured 2026-09-02: step "3" had processed 12.8M tokens and showed
    // nothing, because total_tokens then meant the covered subset.
    const { container } = await mountWithCache({
      b: { cache_hit_tokens: 0, cache_miss_tokens: 0, hit_ratio: null,
           covered_tokens: 0, prompt_tokens: 12_800_000, completion_tokens: 400_000,
           total_tokens: 13_200_000 },
    });
    await waitFor(() => {
      expect(container.querySelector('.cache-inline-badge')).not.toBeNull();
    });
    const text = container.querySelector('.cache-inline-badge')?.textContent ?? '';
    expect(text).toMatch(/13\.2M|13,200,000|13200000/);
    expect(text).not.toContain('%');
  });

  it('still hides the badge when the step processed nothing', async () => {
    const { container } = await mountWithCache({
      b: { cache_hit_tokens: 0, cache_miss_tokens: 0, hit_ratio: null,
           covered_tokens: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    });
    await waitFor(() => {
      expect(container.querySelectorAll('.node').length).toBe(3);
    });
    expect(container.querySelector('.cache-inline-badge')).toBeNull();
  });

  it('the ratio is over the covered subset, the volume is the whole', async () => {
    const { container } = await mountWithCache({
      b: { cache_hit_tokens: 8000, cache_miss_tokens: 2000, hit_ratio: 0.8,
           covered_tokens: 10_000, prompt_tokens: 100_000, completion_tokens: 5_000,
           total_tokens: 105_000 },
    });
    await waitFor(() => {
      expect(container.querySelector('.cache-inline-badge')).not.toBeNull();
    });
    const text = container.querySelector('.cache-inline-badge')?.textContent ?? '';
    expect(text).toContain('80% cache');
    expect(text).toMatch(/105/);          // the whole, not the 10K subset
    expect(text).not.toMatch(/\b10(\.0)?K\b/);
  });
});
