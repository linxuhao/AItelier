/**
 * The graph panel's job is to show the COMPOSED pipeline, with the addon's
 * spliced steps marked. Both halves are worth pinning: an addon that renders
 * identically to its base is exactly the confusion this view exists to remove.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const mockApi = vi.hoisted(() => ({ pipelineGraph: vi.fn() }));
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
