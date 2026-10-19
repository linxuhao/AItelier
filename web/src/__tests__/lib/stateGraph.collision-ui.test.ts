import {expect,it,vi} from 'vitest';
import {render} from '@testing-library/svelte';
import StateGraph from '../../views/StateGraph.svelte';
import {groupFacets,stateLayout} from '../../lib/stateGraph';
import {goal} from '../fixtures/stateProject';
const nodes=()=>[
 goal('x.contract',[],{facet:'contract'}),
 goal('design.bridge',['x.contract'],{facet:'design'}),
 goal('x.test',['design.bridge'],{facet:'test'}),
 goal('x',['x.test','x.contract'],{facet:'content'}),
 goal('x.test.contract',[],{facet:'contract'}),
 goal('x.test.test',['x.test.contract'],{facet:'test'}),
];
it('keeps every original member independently actionable when unsafe parent and nested synthetic key collide',()=>{
 const raw=nodes();expect(stateLayout(raw).nodes).toHaveLength(6);
 const groups=groupFacets(raw);const layout=stateLayout(groups);
 const actionable=groups.flatMap(g=>g.lanes.length ? g.lanes.filter(l=>l.present).map(l=>l.node_key) : g.members);
 console.log(JSON.stringify({groups:groups.map(g=>({key:g.node_key,members:g.members,lanes:g.lanes})),edges:layout.edges,actionable}));
 expect(new Set(actionable)).toEqual(new Set(raw.map(n=>n.node_key)));
});

it('renders an actionable control for the nested test lane on the normal component',()=>{
 const view=render(StateGraph,{nodes:nodes(),selected:'x.test.test',onselect:vi.fn()});
 expect(view.queryByRole('alert')).toBeNull();
 console.log('BUTTON_LABELS',JSON.stringify([...view.container.querySelectorAll('[role="button"]')].map(n=>n.getAttribute('aria-label'))));
 expect(view.queryByRole('button',{name:'x.test.test: OPEN'})).not.toBeNull();
});
