/**
 * The graph panel's job is to show the COMPOSED pipeline, with the addon's
 * spliced steps marked. Both halves are worth pinning: an addon that renders
 * identically to its base is exactly the confusion this view exists to remove.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';

// getTrace: opening a node now also opens its trace pane. It returns an empty
// page here — this file is about the drawing, not the trace.
const mockApi = vi.hoisted(() => ({ pipelineGraph: vi.fn(), getTrace: vi.fn() }));
vi.mock('../../lib/api', () => mockApi);

import PipelineGraph from '../../views/PipelineGraph.svelte';
import realGraph from '../fixtures/pipeline_graph_dpe_game.json';

// dpe_game in miniature: base steps plus a spliced addon step, emitted in the
// order skillflow actually emits a composed graph (overlay steps last).
const composed = {
  config_name: 'dpe_game',
  label: 'DPE Pipeline',
  origin: 'native',
  base: 'dpe_default_v2',
  addons: ['game_harness'],
  addon_steps: ['gh_compile'],
  begin: 'plan',
  steps: [
    { id: 'plan', type: 'agent', checkpoint: true, from_addon: false,
      transitions: [{ to: 'review', match: { from: 'checkpoint', value: 'approved' } }] },
    { id: 'review', type: 'agent', checkpoint: false, from_addon: false,
      transitions: [
        { to: 'gh_compile', match: { from_file: 'v.json', field: 'passed', value: true } },
        { to: 'plan', match: { from_file: 'v.json', field: 'passed', value: false }, max_loop: 3 },
      ] },
    { id: 'done', type: 'gate', from_addon: false, transitions: [{ to: null }] },
    { id: 'gh_compile', type: 'tool', from_addon: true,
      transitions: [{ to: 'done' }] },
  ],
};

describe('PipelineGraph', () => {
  beforeEach(() => vi.resetAllMocks());

  it('draws every step of the composed graph', async () => {
    mockApi.pipelineGraph.mockResolvedValue(composed);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_game' } });
    await waitFor(() => expect(container.querySelectorAll('g.node').length).toBe(4));
    const ids = [...container.querySelectorAll('text.node-id')].map((n) => n.textContent);
    expect(ids).toContain('gh_compile');
  });

  it('marks the addon step so base and combo do not look identical', async () => {
    mockApi.pipelineGraph.mockResolvedValue(composed);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_game' } });
    await waitFor(() => expect(container.querySelectorAll('g.node.addon').length).toBe(1));
    const addon = container.querySelector('g.node.addon');
    expect(addon?.querySelector('text.node-id')?.textContent).toBe('gh_compile');
  });

  it('draws the retry edge as a return, not as forward flow', async () => {
    mockApi.pipelineGraph.mockResolvedValue(composed);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_game' } });
    await waitFor(() => expect(container.querySelectorAll('path.edge').length).toBeGreaterThan(0));
    expect(container.querySelectorAll('path.edge.back')).toHaveLength(1);
    // and its bound is legible, so "loops forever" is never the reading
    const labels = [...container.querySelectorAll('text.edge-label')].map((n) => n.textContent);
    expect(labels).toContain('x3');
  });

  it('marks a checkpoint step', async () => {
    mockApi.pipelineGraph.mockResolvedValue(composed);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_game' } });
    await waitFor(() => expect(container.querySelectorAll('circle.cp-dot').length).toBe(1));
  });

  it('reports a fetch failure instead of rendering an empty box', async () => {
    mockApi.pipelineGraph.mockRejectedValue(new Error('graph is gone'));
    const { findByText } = render(PipelineGraph, { props: { config: 'x' } });
    expect(await findByText('graph is gone')).toBeTruthy();
  });
  it('renders the REAL dpe_game graph, parallel edges and all', async () => {
    // The toy graph above misses what production has: `3_budget -> 3_review`
    // exists twice under different conditions, and the keyed each block threw
    // `each_key_duplicate` on it -- blanking the entire panel for every built-in
    // pipeline. Only a real payload catches that.
    mockApi.pipelineGraph.mockResolvedValue(realGraph);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_game' } });
    await waitFor(() => expect(container.querySelectorAll('g.node').length).toBe(22));
    expect(container.querySelectorAll('g.node.addon')).toHaveLength(
      realGraph.addon_steps.length);
  });
});

describe('PipelineGraph with a run folded on', () => {
  beforeEach(() => vi.resetAllMocks());

  // A loop graph in miniature: `body` runs once per item inside `task_loop`.
  const loopGraph = {
    config_name: 'dpe_default_v2', label: 'DPE', origin: 'native',
    base: 'dpe_default_v2', addons: [], addon_steps: [], begin: 'setup',
    loops: { task_loop: ['body'] },
    steps: [
      { id: 'setup', type: 'tool', transitions: [{ to: 'task_loop' }] },
      { id: 'task_loop', type: 'loop', is_loop: true,
        transitions: [{ to: 'body', max_loop: 100 }, { to: 'wrap' }] },
      { id: 'body', type: 'agent', loop_id: 'task_loop',
        transitions: [{ to: 'task_loop' }] },
      { id: 'wrap', type: 'agent', transitions: [] },
    ],
  };

  const rows = [
    { id: 1, step_id: 'setup', status: 'completed', loop_item: null },
    { id: 2, step_id: 'body', status: 'completed', loop_item: 'alpha',
      claimed_at: '2026-08-25T10:00:00Z', completed_at: '2026-08-25T10:00:20Z' },
    { id: 3, step_id: 'body', status: 'failed', loop_item: 'beta', error: 'nope' },
    { id: 4, step_id: 'wrap', status: 'pending', loop_item: null },
  ];

  it('draws the loop body inside its own box', async () => {
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container } = render(PipelineGraph,
      { props: { config: 'dpe_default_v2', runSteps: rows } });
    await waitFor(() => expect(container.querySelector('g.loop-box')).toBeTruthy());
    expect(container.querySelector('g.loop-box text')?.textContent?.trim())
      .toContain('task_loop');
  });

  it('offers every loop item this run actually claimed', async () => {
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container, findByText } = render(PipelineGraph,
      { props: { config: 'dpe_default_v2', runSteps: rows } });
    expect(await findByText('alpha')).toBeTruthy();
    expect(await findByText('beta')).toBeTruthy();
    expect(container.querySelectorAll('.item-chip').length).toBe(3);  // all + 2
  });

  it('reports the SELECTED item state, not the aggregate', async () => {
    // Unfiltered the body is failed (beta blew up). Picking alpha must show
    // alpha's own outcome -- that is the entire point of the picker.
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container, findByText } = render(PipelineGraph,
      { props: { config: 'dpe_default_v2', runSteps: rows } });
    await waitFor(() => expect(container.querySelector('g.node.failed')).toBeTruthy());
    await fireEvent.click(await findByText('alpha'));
    await waitFor(() => expect(container.querySelector('g.node.failed')).toBeNull());
    expect(container.querySelector('g.node.completed')).toBeTruthy();
  });

  it('colours a node by run status and leaves an unrun one dashed', async () => {
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container } = render(PipelineGraph,
      { props: { config: 'dpe_default_v2', runSteps: rows } });
    await waitFor(() => expect(container.querySelectorAll('g.node.completed').length)
      .toBeGreaterThan(0));
    expect(container.querySelectorAll('g.node.pending').length).toBeGreaterThan(0);
  });

  it('opens a node to the instances behind it, with their items', async () => {
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    mockApi.getTrace.mockResolvedValue({ traces: [], has_more: false });
    const { container } = render(PipelineGraph,
      { props: { config: 'dpe_default_v2', runSteps: rows, runId: 'r1' } });
    await waitFor(() => expect(container.querySelectorAll('g.node').length).toBe(4));
    const body = [...container.querySelectorAll('g.node')]
      .find((g) => g.querySelector('text.node-id')?.textContent === 'body');
    await fireEvent.click(body as Element);
    const pane = await waitFor(() => {
      const el = container.querySelector('.trace-pane');
      expect(el).toBeTruthy();
      return el as Element;
    });
    // Both instances are offered by item, and the pane reads ONE of them --
    // the latest, whose failure is the thing worth surfacing.
    const chips = [...pane.querySelectorAll('.tp-instances .item-chip')]
      .map((c) => c.textContent?.trim());
    expect(chips.some((c) => c?.includes('alpha'))).toBe(true);
    expect(chips.some((c) => c?.includes('beta'))).toBe(true);
    expect(pane.querySelector('.exec-error')?.textContent).toBe('nope');
    // ...and the trace it read is that instance's, not the step's in general.
    expect(mockApi.getTrace.mock.calls.at(-1)?.[1].stepInstanceId).toBe(3);
  });

  it('says items were not recorded rather than inventing one', async () => {
    // A run from before skillflow stamped loop_item: body rows exist, none has
    // an item. Showing a single nameless chip would claim a one-item fan-out.
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container, findByText } = render(PipelineGraph, {
      props: {
        config: 'dpe_default_v2',
        runSteps: [
          { id: 1, step_id: 'body', status: 'completed', loop_item: null },
          { id: 2, step_id: 'body', status: 'completed', loop_item: null },
        ],
      },
    });
    await waitFor(() => expect(container.querySelector('.item-note')).toBeTruthy());
    expect(container.querySelectorAll('.item-chip')).toHaveLength(0);
    // and the graph still renders -- an old run's page must not go blank
    expect(container.querySelectorAll('g.node').length).toBe(4);
    expect(await findByText('body')).toBeTruthy();
  });

  it('stays a plain config view when no run is passed', async () => {
    mockApi.pipelineGraph.mockResolvedValue(loopGraph);
    const { container } = render(PipelineGraph, { props: { config: 'dpe_default_v2' } });
    await waitFor(() => expect(container.querySelectorAll('g.node').length).toBe(4));
    expect(container.querySelector('.item-strip')).toBeNull();
    expect(container.querySelector('g.node.clickable')).toBeNull();
  });
});
