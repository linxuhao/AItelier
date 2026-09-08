import { describe, it, expect } from 'vitest';
import { stateLayout, stateTone, stateProjectHref, exactRunHref, cardTitle, cardLines, STATE_CARD } from '../../lib/stateGraph';
import { goal } from '../fixtures/stateProject';

describe('State DAG layout semantics', () => {
  it('draws prerequisites toward dependents, not the reverse', () => {
    const r = stateLayout([goal('b', ['a']), goal('a')]);
    expect(r.edges.map(e => [e.from,e.to])).toEqual([['a','b']]);
    expect(r.nodes.find(n => n.node_key === 'a')!.y).toBeLessThan(r.nodes.find(n => n.node_key === 'b')!.y);
  });
  it('does not silently discard cycles or dangling dependencies', () => {
    expect(() => stateLayout([goal('a',['b']), goal('b',['a'])])).toThrow(/cycle/);
    expect(() => stateLayout([goal('a',['missing'])])).toThrow(/Missing/);
    expect(() => stateLayout([goal('a'),goal('a')])).toThrow(/Duplicate/);
  });
  it('shows outside dependency counts instead of inventing filtered edges', () => {
    const r = stateLayout([goal('growth.a'),goal('month.b',['growth.a'])], 'month');
    expect(r.nodes).toHaveLength(1); expect(r.edges).toHaveLength(0);
    expect(r.hiddenEdges).toBe(1); expect(r.nodes[0].outsideDependencies).toBe(1);
  });
  it('focuses only the selected goal and its actual neighbours', () => {
    const r = stateLayout([goal('a'),goal('b',['a']),goal('c',['b']),goal('d')], '', 'b');
    expect(r.nodes.map(n => n.node_key).sort()).toEqual(['a','b','c']);
  });
  it('refuses an unreadable giant overview until the user narrows the view', () => {
    const nodes = Array.from({length: 61},(_,i) => goal('area.'+i));
    expect(stateLayout(nodes).tooLarge).toBe(true);
    expect(stateLayout(nodes,'','area.0').tooLarge).toBe(false);
  });
  it('never treats a completed attempt as a verified goal', () => {
    expect(stateTone('completed')).not.toBe('verified');
    expect(stateTone('CANDIDATE')).toBe('candidate');
    expect(stateTone('VERIFIED')).toBe('verified');
  });
  it('encodes exact identities and bounds mixed-width labels', () => {
    expect(stateProjectHref('a/b','c d')).toBe('#/state-projects/a%2Fb/nodes/c%20d');
    expect(exactRunHref('run#1')).toBe('#/state-runs/run%231');
    expect(cardTitle('武虾传奇'.repeat(15)).length).toBeLessThanOrEqual(15);
    expect(cardTitle('abcdef')).toBe('abcdef');
  });
});


describe('Readable node cards', () => {
  it('wraps CJK and Latin titles into two lines with explicit truncation', () => {
    expect(cardLines('短标题')).toEqual(['短标题']);
    const cjk=cardLines('武虾传奇'.repeat(50));
    expect(cjk).toHaveLength(2); expect(cjk[1].endsWith('…')).toBe(true);
    expect(cjk.every(s=>Array.from(s).length <= 18)).toBe(true);
    expect(cardLines('A'.repeat(200))).toHaveLength(2);
  });
  it('allocates separate title, fact, readiness and attempt rows', () => {
    expect(STATE_CARD.height).toBeGreaterThanOrEqual(156);
    const layout=stateLayout([goal('a'),goal('b',['a'])]);
    expect(layout.nodes[1].y-layout.nodes[0].y).toBeGreaterThan(STATE_CARD.height);
  });
});
