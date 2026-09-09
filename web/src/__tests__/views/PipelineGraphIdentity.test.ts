/**
 * Graph structure is keyed by exact run identity (or config in preview mode),
 * not by fresh run snapshots. ProjectRunLive.test.ts exercises the real parent
 * snapshot replacement; these checks cover genuine identity changes and races.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import PipelineGraph from '../../views/PipelineGraph.svelte';

const mockApi = vi.hoisted(() => ({
  pipelineGraph: vi.fn(), runWorkflowGraph: vi.fn(), getTrace: vi.fn(),
}));
vi.mock('../../lib/api', () => mockApi);

function graph(node: string) {
  return {
    graph_version: 1, begin: node, loops: {}, node_labels: {},
    steps: [{ id: node, type: 'tool', transitions: [{ to: null }] }],
  };
}

function deferredGraph() {
  type Settlers = Parameters<ConstructorParameters<typeof Promise<ReturnType<typeof graph>>>[0]>;
  let resolve!: Settlers[0];
  let reject!: Settlers[1];
  const promise = new Promise<ReturnType<typeof graph>>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

async function expectNode(container: HTMLElement, id: string) {
  await waitFor(() => expect(container.querySelector('text.node-id')?.textContent).toBe(id));
}

describe('PipelineGraph identity', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockApi.pipelineGraph.mockResolvedValue(graph('preview'));
    mockApi.runWorkflowGraph.mockResolvedValue(graph('pinned'));
    mockApi.getTrace.mockResolvedValue({ traces: [], has_more: false });
  });

  it('ignores config changes while the exact run stays the same', async () => {
    const { container, rerender } = render(PipelineGraph, {
      props: { config: 'old-name', runId: 'run-one' },
    });
    await expectNode(container, 'pinned');
    const svg = container.querySelector('svg');

    await rerender({ config: 'new-name', runId: 'run-one' });
    await tick();
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledTimes(1);
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledWith('run-one');
    expect(mockApi.pipelineGraph).not.toHaveBeenCalled();
    expect(container.querySelector('svg')).toBe(svg);
  });

  it('loads a new exact run even when its config is unchanged', async () => {
    const { container, rerender } = render(PipelineGraph, {
      props: { config: 'same-config', runId: 'run-one' },
    });
    await expectNode(container, 'pinned');
    mockApi.runWorkflowGraph.mockResolvedValue(graph('second-run'));

    await rerender({ config: 'same-config', runId: 'run-two' });
    await expectNode(container, 'second-run');
    expect(mockApi.runWorkflowGraph.mock.calls.map(([id]) => id)).toEqual(['run-one', 'run-two']);
    expect(mockApi.pipelineGraph).not.toHaveBeenCalled();
  });

  it('reloads a config preview only when the config changes', async () => {
    const { container, rerender } = render(PipelineGraph, { props: { config: 'first-config' } });
    await expectNode(container, 'preview');
    const svg = container.querySelector('svg');
    await rerender({ config: 'first-config', labels: { preview: 'New label' } });
    await tick();
    expect(mockApi.pipelineGraph).toHaveBeenCalledTimes(1);
    expect(container.querySelector('svg')).toBe(svg);

    mockApi.pipelineGraph.mockResolvedValue(graph('second-preview'));
    await rerender({ config: 'second-config' });
    await expectNode(container, 'second-preview');
    expect(mockApi.pipelineGraph.mock.calls.map(([name]) => name)).toEqual(['first-config', 'second-config']);
    expect(mockApi.runWorkflowGraph).not.toHaveBeenCalled();
  });

  it('keeps run and config identities distinct even when their strings match', async () => {
    const { container, rerender } = render(PipelineGraph, { props: { config: 'shared-id' } });
    await expectNode(container, 'preview');
    await rerender({ config: 'shared-id', runId: 'shared-id' });
    await expectNode(container, 'pinned');
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledWith('shared-id');

    mockApi.pipelineGraph.mockResolvedValue(graph('back-to-preview'));
    await rerender({ config: 'shared-id', runId: undefined });
    await expectNode(container, 'back-to-preview');
    expect(mockApi.pipelineGraph).toHaveBeenCalledTimes(2);
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledTimes(1);
  });

  it.each(['resolve', 'reject'] as const)('ignores a stale graph %s after switching runs', async (outcome) => {
    const first = deferredGraph();
    mockApi.runWorkflowGraph.mockReturnValueOnce(first.promise).mockResolvedValue(graph('current-run'));
    const { container, rerender } = render(PipelineGraph, {
      props: { config: 'same-config', runId: 'run-one' },
    });
    await waitFor(() => expect(mockApi.runWorkflowGraph).toHaveBeenCalledWith('run-one'));
    await rerender({ config: 'same-config', runId: 'run-two' });
    await expectNode(container, 'current-run');
    const svg = container.querySelector('svg');

    if (outcome === 'resolve') first.resolve(graph('stale-run'));
    else first.reject(new Error('stale failure'));
    await Promise.resolve();
    await tick();
    expect(container.querySelector('text.node-id')?.textContent).toBe('current-run');
    expect(container.querySelector('.graph-error')).toBeNull();
    expect(container.querySelector('svg')).toBe(svg);
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledTimes(2);
  });

  it('does not replace a failed exact-run graph with the latest config graph', async () => {
    mockApi.runWorkflowGraph.mockRejectedValueOnce(new Error('Pinned graph unavailable'));
    const { container, rerender } = render(PipelineGraph, {
      props: { config: 'same-config', runId: 'run-one' },
    });
    await waitFor(() => expect(container.querySelector('.graph-error')?.textContent).toBe('Pinned graph unavailable'));
    expect(mockApi.pipelineGraph).not.toHaveBeenCalled();

    await rerender({ config: 'same-config', runId: 'run-two' });
    await expectNode(container, 'pinned');
    expect(container.querySelector('.graph-error')).toBeNull();
    expect(mockApi.runWorkflowGraph).toHaveBeenCalledTimes(2);
  });
});
