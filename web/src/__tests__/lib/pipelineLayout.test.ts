import { describe, it, expect } from 'vitest';
import { layoutGraph, edgeLabel, type GraphStep } from '../../lib/pipelineLayout';

/**
 * The layout exists to survive REAL pipeline graphs, and every real one has a
 * review loop in it. The tests below are shaped around the two things that can
 * actually break: a cycle that must not hang the ranker, and an addon whose
 * spliced steps arrive at the END of the step list yet belong in the middle of
 * the flow.
 */

const linear: GraphStep[] = [
  { id: 'a', type: 'tool', transitions: [{ to: 'b' }] },
  { id: 'b', type: 'agent', transitions: [{ to: 'c' }] },
  { id: 'c', type: 'gate', transitions: [{ to: null }] },
];

// b -> b_review -> c, with b_review -> b as a bounded retry. This is the DPE
// Green/Red pair, and it is a cycle.
const withLoop: GraphStep[] = [
  { id: 'a', transitions: [{ to: 'b' }] },
  {
    id: 'b',
    transitions: [{ to: 'b_review', match: { from: 'checkpoint', value: 'approved' } }],
  },
  {
    id: 'b_review',
    transitions: [
      { to: 'c', match: { from_file: 'review_verdict.json', field: 'passed', value: true } },
      {
        to: 'b',
        match: { from_file: 'review_verdict.json', field: 'passed', value: false },
        max_loop: 3,
      },
    ],
  },
  { id: 'c', transitions: [{ to: null }] },
];

describe('layoutGraph', () => {
  it('ranks a linear chain in order', () => {
    const l = layoutGraph(linear, 'a');
    expect(l.nodes.map((n) => n.id)).toEqual(['a', 'b', 'c']);
    expect(l.nodes.map((n) => n.rank)).toEqual([0, 1, 2]);
    expect(l.rankCount).toBe(3);
    expect(l.widest).toBe(1);
  });

  it('terminates on a review loop and ranks it forward', () => {
    // Without back-edge classification the longest-path relaxation on b <-> b_review
    // never settles; the bounded pass count would cap it but produce nonsense ranks.
    const l = layoutGraph(withLoop, 'a');
    const rank = Object.fromEntries(l.nodes.map((n) => [n.id, n.rank]));
    expect(rank.a).toBe(0);
    expect(rank.b).toBe(1);
    expect(rank.b_review).toBe(2);
    expect(rank.c).toBe(3);
  });

  it('marks the retry edge as a back edge and keeps its bound', () => {
    const l = layoutGraph(withLoop, 'a');
    const retry = l.edges.find((e) => e.from === 'b_review' && e.to === 'b');
    expect(retry?.back).toBe(true);
    expect(retry?.maxLoop).toBe(3);
    // The forward edges must NOT be marked back, or the flow renders as returns.
    expect(l.edges.filter((e) => e.back)).toHaveLength(1);
  });

  it('drops transitions to null and to unknown steps', () => {
    const l = layoutGraph(
      [
        { id: 'a', transitions: [{ to: 'b' }, { to: null }, { to: 'ghost' }] },
        { id: 'b', transitions: [] },
      ],
      'a',
    );
    expect(l.edges).toEqual([
      { from: 'a', to: 'b', back: false, maxLoop: null, label: '' },
    ]);
  });

  it('places an addon step by its edges, not by its position in the list', () => {
    // How skillflow actually emits a composed graph: the overlay's steps are
    // appended, so list order says nothing about where they run.
    const composed: GraphStep[] = [
      { id: 'start', transitions: [{ to: 'spliced' }] },
      { id: 'end', transitions: [{ to: null }] },
      { id: 'spliced', from_addon: true, transitions: [{ to: 'end' }] },
    ];
    const l = layoutGraph(composed, 'start');
    expect(l.nodes.map((n) => n.id)).toEqual(['start', 'spliced', 'end']);
    expect(l.nodes[1].from_addon).toBe(true);
  });

  it('still lays out steps unreachable from begin instead of dropping them', () => {
    // A dangling failure gate is a real shape; silently omitting it would make
    // the picture claim the pipeline has no such exit.
    const l = layoutGraph(
      [
        { id: 'a', transitions: [{ to: 'b' }] },
        { id: 'b', transitions: [] },
        { id: 'orphan', transitions: [] },
      ],
      'a',
    );
    expect(l.nodes.map((n) => n.id).sort()).toEqual(['a', 'b', 'orphan']);
  });

  it('puts sibling branches on the same rank and widens', () => {
    const l = layoutGraph(
      [
        { id: 'a', transitions: [{ to: 'x' }, { to: 'y' }] },
        { id: 'x', transitions: [] },
        { id: 'y', transitions: [] },
      ],
      'a',
    );
    expect(l.widest).toBe(2);
    const rank = Object.fromEntries(l.nodes.map((n) => [n.id, n.rank]));
    expect(rank.x).toBe(1);
    expect(rank.y).toBe(1);
  });

  it('falls back to the first step when begin names nothing', () => {
    const l = layoutGraph(linear, 'not_a_step');
    expect(l.nodes[0].id).toBe('a');
  });

  it('handles an empty graph without throwing', () => {
    expect(layoutGraph([], 'a').nodes).toEqual([]);
  });

  it('collapses parallel edges into one and keeps both conditions', () => {
    // dpe_default_v2 really does route 3_budget -> 3_review two ways. Drawn
    // separately they stack into one illegible curve, and they broke the
    // component's keyed each block outright.
    const l = layoutGraph(
      [
        {
          id: 'budget',
          transitions: [
            { to: 'review', match: { ok: true } },
            { to: 'plan', match: { ok: false }, max_loop: 2 },
            { to: 'review', match: { skipped: true } },
          ],
        },
        { id: 'plan', transitions: [{ to: 'budget' }] },
        { id: 'review', transitions: [] },
      ],
      'plan',
    );
    const parallel = l.edges.filter((e) => e.from === 'budget' && e.to === 'review');
    expect(parallel).toHaveLength(1);
    expect(parallel[0].label).toBe('ok=true / skipped=true');
  });

  it('keeps a loop bound when the parallel edge it merged with had none', () => {
    const l = layoutGraph(
      [
        { id: 'a', transitions: [{ to: 'b' }] },
        {
          id: 'b',
          transitions: [
            { to: 'a', match: { x: 1 } },
            { to: 'a', match: { y: 2 }, max_loop: 4 },
          ],
        },
      ],
      'a',
    );
    const back = l.edges.filter((e) => e.back);
    expect(back).toHaveLength(1);
    expect(back[0].maxLoop).toBe(4);
  });
});

describe('edgeLabel', () => {
  it('reads a checkpoint edge as its verdict', () => {
    expect(edgeLabel({ to: 'x', match: { from: 'checkpoint', value: 'approved' } }))
      .toBe('approved');
  });

  it('reads a verdict-file edge as field=value', () => {
    expect(
      edgeLabel({
        to: 'x',
        match: { from_file: 'review_verdict.json', field: 'passed', value: false },
      }),
    ).toBe('passed=false');
  });

  it('names the error edge', () => {
    expect(edgeLabel({ to: 'x', match: { _error: true } })).toBe('on error');
  });

  it('is empty for an unconditional edge, so nothing is drawn', () => {
    expect(edgeLabel({ to: 'x' })).toBe('');
    expect(edgeLabel({ to: 'x', match: {} })).toBe('');
  });

  it('shows a plain output match as written', () => {
    expect(edgeLabel({ to: 'x', match: { synced: true } })).toBe('synced=true');
  });
});
