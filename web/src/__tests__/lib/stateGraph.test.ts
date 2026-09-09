import { describe, it, expect } from 'vitest';
import { stateLayout, stateTone, stateNextAction, stateReadyActionCounts, stateProjectHref, exactRunHref,
         cardTitle, cardLines, groupFacets, stemOf, STATE_CARD, type StateNodeSummary } from '../../lib/stateGraph';
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
  it('uses server next actions when present and safely derives them during a rolling deployment', () => {
    const candidate = goal('candidate', [], {status: 'CANDIDATE', readiness: 'ready'});
    const open = goal('open', [], {status: 'OPEN', readiness: 'ready'});
    const stale = goal('stale', [], {status: 'STALE', readiness: 'ready'});
    const held = goal('held', [], {status: 'CANDIDATE', readiness: 'held'});
    expect(stateNextAction(candidate)).toBe('candidate_review');
    expect(stateNextAction(open)).toBe('new_attempt');
    expect(stateNextAction(stale)).toBe('new_attempt');
    expect(stateNextAction(held)).toBeNull();
    expect(stateNextAction({...open, next_action: 'candidate_review'})).toBe('candidate_review');
    expect(stateReadyActionCounts([candidate, open, stale, held])).toEqual({candidate_review: 1, new_attempt: 2});
    expect(stateReadyActionCounts([candidate], {candidate_review: 5, new_attempt: 2})).toEqual({candidate_review: 5, new_attempt: 2});
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

describe('groupFacets', () => {
  const n = (key: string, facet: string | null, deps: string[] = [], status = 'OPEN'): StateNodeSummary => ({
    node_key: key, title: 'T ' + key, domain: key.split('.')[0], revision: 1, contract_hash: 'a'.repeat(64),
    facet, status, readiness: 'ready', dependencies: deps, blocked_by: [], hold: null,
    node_hold: { revision: 0, held: 0, reason: '' }, criteria_count: 1, attempt_count: 0,
    latest_attempt: null, latest_evidence: {}, priority: 0,
  });
  const family = [
    n('a.contract', 'contract', [], 'VERIFIED'),
    n('a.test', 'test', ['a.contract']),
    n('a', 'content', ['a.contract', 'a.test']),
    n('b.contract', 'contract', ['a.contract']),
    n('b', 'content', ['b.contract', 'a.contract']),
    n('d', 'design', [], 'VERIFIED'),
    n('v', 'integration', ['a', 'b']),
  ];

  it('collapses three lanes into one node and drops the internal edges', () => {
    const groups = groupFacets(family);
    expect(groups.map(g => g.node_key).sort()).toEqual(['a', 'b', 'd', 'v']);
    const a = groups.find(g => g.node_key === 'a')!;
    expect(a.members).toEqual(['a', 'a.contract', 'a.test']);
    expect(a.dependencies).toEqual([]);                       // a.contract/a.test are internal
    expect(groups.find(g => g.node_key === 'b')!.dependencies).toEqual(['a']);  // re-pointed at the group
    expect(groups.find(g => g.node_key === 'v')!.dependencies).toEqual(['a', 'b']);
  });

  it('reports every lane, including one that does not exist yet', () => {
    const a = groupFacets(family).find(g => g.node_key === 'a')!;
    expect(a.lanes.map(l => [l.facet, l.status, l.present, l.revision])).toEqual([
      ['contract', 'VERIFIED', true, 1], ['test', 'OPEN', true, 1], ['content', 'OPEN', true, 1]]);
    // An absent lane carries no revision to render.
    expect(groupFacets(family).find(g => g.node_key === 'b')!.lanes[1].revision).toBe(0);
    const b = groupFacets(family).find(g => g.node_key === 'b')!;
    expect(b.lanes.map(l => [l.facet, l.present])).toEqual([
      ['contract', true], ['test', false], ['content', true]]);
    expect(b.lanes[1].node_key).toBe('b.test');
  });

  it('carries the lane worth acting on, so a click lands on real work', () => {
    // a.contract is VERIFIED, so the group speaks for the test lane.
    const a = groupFacets(family).find(g => g.node_key === 'a')!;
    expect(a.facet).toBe('test');
    // Everything done: the group speaks for the implementation.
    const done = family.map(x => x.node_key.startsWith('a') ? { ...x, status: 'VERIFIED' } : x);
    expect(groupFacets(done).find(g => g.node_key === 'a')!.facet).toBe('content');
  });

  it('leaves a node with no siblings alone and gives it no lanes', () => {
    const groups = groupFacets(family);
    expect(groups.find(g => g.node_key === 'd')!.lanes).toEqual([]);
    expect(groups.find(g => g.node_key === 'v')!.lanes).toEqual([]);
    expect(groups.find(g => g.node_key === 'd')!.members).toEqual(['d']);
  });

  it('keeps the goal title, not the generated contract prose', () => {
    const withContractTitle = family.map(x =>
      x.node_key === 'a.contract' ? { ...x, title: '「T a」的接口契约' } : x);
    expect(groupFacets(withContractTitle).find(g => g.node_key === 'a')!.title).toBe('T a');
  });

  it('does not strip a suffix the facet does not claim', () => {
    expect(stemOf({ node_key: 'x.test', facet: null })).toBe('x.test');
    expect(stemOf({ node_key: 'x.test', facet: 'test' })).toBe('x');
    expect(stemOf({ node_key: 'coop.world-facts.contract', facet: 'contract' })).toBe('coop.world-facts');
    // A legacy node literally named "x.test" is its own goal, not a lane.
    const groups = groupFacets([n('x.test', null), n('x.test.contract', 'contract')]);
    expect(groups.map(g => g.node_key).sort()).toEqual(['x.test']);
    expect(groups[0].members).toEqual(['x.test', 'x.test.contract']);
  });

  it('the collapsed graph still lays out', () => {
    const layout = stateLayout(groupFacets(family));
    expect(layout.nodes.map(x => x.node_key).sort()).toEqual(['a', 'b', 'd', 'v']);
    expect(layout.tooLarge).toBe(false);
  });
});
