import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { overview, detail, attempt, goal } from '../fixtures/stateProject';

const api = vi.hoisted(() => ({ stateOverview: vi.fn(), stateNode: vi.fn(), stateAttempts: vi.fn(),
  stateRefreshProject: vi.fn(), stateAttemptDetail: vi.fn(), stateProjects: vi.fn(), stateRunOwners: vi.fn(),
  getRunDetail: vi.fn(), pipelineGraph: vi.fn(), runWorkflowGraph: vi.fn(), getTrace: vi.fn(), setUserLang: vi.fn() }));
vi.mock('../../lib/api', () => api);
import StateGraph from '../../views/StateGraph.svelte';
import StateProject from '../../views/StateProject.svelte';
import StateProjects from '../../views/StateProjects.svelte';
import StateNodePanel from '../../views/StateNodePanel.svelte';
import StateRun from '../../views/StateRun.svelte';
import RunStateLinks from '../../views/RunStateLinks.svelte';
import RelatedStateProjects from '../../views/RelatedStateProjects.svelte';

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'writer@local'});
  api.stateOverview.mockResolvedValue(overview());
  api.stateNode.mockImplementation(async (_p,key) => detail(key));
  api.stateAttempts.mockResolvedValue({attempts:[attempt()], next_after:null});
  api.stateAttemptDetail.mockResolvedValue({attempt: {...attempt(), context:{}}, evidence:[], receipts:[], evidence_truncated:false});
  api.stateProjects.mockResolvedValue({projects:[], next_after:null});
  api.stateRunOwners.mockResolvedValue({links:[]});
  api.getTrace.mockResolvedValue({traces:[], has_more:false});
  api.runWorkflowGraph.mockResolvedValue({begin:'historical', steps:[{id:'historical',type:'agent',transitions:[]}], graph_version:2, node_labels:{historical:'historical'}});
});

describe('State graph component', () => {
  it('separates goal facts from attempt completion and exposes keyboard selection', async () => {
    const click = vi.fn();
    const nodes = [goal('growth.a',[],{status:'CANDIDATE', latest_attempt:attempt({status:'completed'})}), goal('month.b',['growth.a'])];
    const view = render(StateGraph,{nodes,selected:'',onselect:click});
    expect(view.container.querySelectorAll('.goal.verified')).toHaveLength(0);
    expect(view.container.querySelectorAll('.goal.candidate')).toHaveLength(1);
    await fireEvent.keyDown(view.container.querySelector('g.goal')!,{key:'Enter'});
    expect(click).toHaveBeenCalledWith('growth.a');
    await fireEvent.click(view.getByRole('button',{name:'Zoom in'}));
    expect(view.getByRole('button',{name:'120%'})).toBeTruthy();
  });
  it('filters domains without claiming boundary dependencies vanished', async () => {
    const view=render(StateGraph,{nodes:[goal('growth.a'),goal('month.b',['growth.a'])],selected:'',onselect:vi.fn()});
    await fireEvent.change(view.getByRole('combobox'),{target:{value:'month'}});
    expect(view.container.querySelectorAll('g.goal')).toHaveLength(1);
    expect(view.container.textContent).toContain('Edges to goals outside this view: 1');
    expect(view.container.querySelectorAll('path.dependency')).toHaveLength(0);
  });
  it('shows malformed-graph and empty states rather than a misleading diagram', () => {
    const bad=render(StateGraph,{nodes:[goal('a',['missing'])],selected:'',onselect:vi.fn()});
    expect(bad.getByRole('alert').textContent).toContain('Missing'); bad.unmount();
    const empty=render(StateGraph,{nodes:[],selected:'',onselect:vi.fn()});
    expect(empty.getByText('No goals in this project.')).toBeTruthy();
  });
});

describe('Long-lived project pages', () => {
  it('never fetches private data for a reader', async () => {
    authStore.set({canWrite:false, permissionResolved:true, email:null});
    const view=render(StateProject,{params:{id:'game'}});
    expect(view.getByText(/Project state is private/)).toBeTruthy();
    await new Promise(r=>setTimeout(r,0));
    expect(api.stateOverview).not.toHaveBeenCalled(); expect(api.stateNode).not.toHaveBeenCalled();
  });
  it('shows project hold and candidate without granting verified status', async () => {
    const view=render(StateProject,{params:{id:'game'}});
    await view.findByRole('heading',{name:'武虾传奇'});
    expect(view.container.textContent).toContain('Migration review; no new dispatch');
    expect(view.container.querySelectorAll('.goal.verified')).toHaveLength(0);
    expect(view.container.textContent).toContain('CANDIDATE');
    expect(api.stateRefreshProject).not.toHaveBeenCalled();
  });
  it('preserves user selection across view reload on an initial node permalink', async () => {
    const view=render(StateProject,{params:{id:'game',nodeKey:'growth.progress'}});
    await view.findByRole('heading',{name:'武虾传奇'});
    const second=[...view.container.querySelectorAll('g.goal')].find(n=>n.getAttribute('aria-label')?.startsWith('Monthly actions'))!;
    await fireEvent.click(second);
    await waitFor(()=>expect(view.container.querySelector('.node-panel')?.textContent).toContain('Detailed requirement for month.actions'));
    await fireEvent.click(view.getByRole('button',{name:'Reload view'}));
    await waitFor(()=>expect(api.stateOverview).toHaveBeenCalledTimes(2));
    await waitFor(()=>expect(view.container.querySelector('.node-panel')?.textContent).toContain('Detailed requirement for month.actions'));
    expect(api.stateRefreshProject).not.toHaveBeenCalled();
  });
  it('observes workflow outcomes only on explicit sync and handles next batch', async () => {
    api.stateRefreshProject.mockResolvedValueOnce({results:[{attempt_id:'attempt-one',error:'StateConflict'}],next_after:10})
      .mockResolvedValueOnce({results:[],next_after:null});
    const view=render(StateProject,{params:{id:'game'}});
    await view.findByRole('heading',{name:'武虾传奇'});
    await fireEvent.click(view.getByRole('button',{name:'Sync run results'}));
    await waitFor(()=>expect(api.stateRefreshProject).toHaveBeenCalledWith('game',0));
    await waitFor(()=>expect(view.container.textContent).toContain('Observation errors: 1'));
    await fireEvent.click(view.getByRole('button',{name:'Sync next batch'}));
    await waitFor(()=>expect(api.stateRefreshProject).toHaveBeenLastCalledWith('game',10));
  });
  it('loads project attempts lazily and links the exact run', async () => {
    const view=render(StateProject,{params:{id:'game'}});
    await view.findByRole('heading',{name:'武虾传奇'});
    expect(api.stateAttempts).not.toHaveBeenCalled();
    await fireEvent.click(view.getByRole('button',{name:'Runs / attempts'}));
    await waitFor(()=>expect(api.stateAttempts).toHaveBeenCalledWith('game'));
    expect((await view.findByRole('link',{name:/Workflow graph/})).getAttribute('href')).toBe('#/state-runs/run-one');
    expect(view.container.querySelector('a[href="#/state-runs/run-one"]')).toBeTruthy();
  });
  it('does not treat a failed refresh as an empty, successful project', async () => {
    api.stateOverview.mockRejectedValueOnce(new Error('503 service unavailable'));
    const view=render(StateProject,{params:{id:'game'}});
    expect(await view.findByRole('alert')).toHaveProperty('textContent',expect.stringContaining('503'));
    await fireEvent.click(view.getByRole('button',{name:'Retry'}));
    await view.findByRole('heading',{name:'武虾传奇'});
  });
  it('clears private content when access is revoked', async () => {
    const view=render(StateProject,{params:{id:'game'}});
    await view.findByRole('heading',{name:'武虾传奇'});
    authStore.set({canWrite:false, permissionResolved:true, email:null});
    await waitFor(()=>expect(view.queryByRole('heading',{name:'武虾传奇'})).toBeNull());
    expect(view.container.querySelector('.node-panel')).toBeNull();
  });
  it('catalog distinguishes no migration from legacy project existence', async () => {
    const view=render(StateProjects);
    expect(await view.findByText(/No state projects yet/)).toBeTruthy();
    expect(api.stateProjects).toHaveBeenCalledWith(undefined);
  });
  it('catalog and repo links refer to the same project identity', async () => {
    const o=overview();
    api.stateProjects.mockResolvedValue({projects:[{...o.project,node_count:2,verified_count:0,source:o.source,policy:o.policy}],next_after:null});
    const view=render(RelatedStateProjects,{repoPath:'/private/source'});
    expect(await view.findByRole('link',{name:/武虾传奇/})).toHaveProperty('hash','#/state-projects/game');
    expect(api.stateProjects).toHaveBeenCalledWith('/private/source');
  });
});

describe('Detail races and exact run navigation', () => {
  it('late response for a previous node cannot overwrite the newly selected node', async () => {
    let resolveFirst: (v: unknown)=>void=()=>{};
    api.stateNode.mockImplementation((_p,k)=>k==='growth.progress' ? new Promise(r=>resolveFirst=r) : Promise.resolve(detail(k)));
    const view=render(StateNodePanel,{projectId:'game',nodeKey:'growth.progress',onselect:vi.fn()});
    await waitFor(()=>expect(api.stateNode).toHaveBeenCalledTimes(1));
    await view.rerender({projectId:'game',nodeKey:'month.actions',onselect:vi.fn()});
    await view.findByText(/Detailed requirement for month.actions/);
    resolveFirst(detail('growth.progress'));
    await new Promise(r=>setTimeout(r,0));
    expect(view.container.textContent).not.toContain('Detailed requirement for growth.progress');
  });
  it('run page renders the exact pinned graph, not the current config', async () => {
    api.getRunDetail.mockResolvedValue({id:'run-old',config_name:'feature',project_id:'sg-one',status:'completed',steps:[]});
    const view=render(StateRun,{params:{runId:'run-old'}});
    await waitFor(()=>expect(api.runWorkflowGraph).toHaveBeenCalledWith('run-old'));
    expect(api.pipelineGraph).not.toHaveBeenCalled();
    await waitFor(()=>expect(view.container.textContent).toContain('graph v2'));
    expect(view.container.querySelector('a[href="#/projects/sg-one/trace/run-old"]')).toBeTruthy();
  });
  it('does not fall back when pinned history is unavailable', async () => {
    api.getRunDetail.mockResolvedValue({id:'run-old',graph_name:'feature',project_id:'sg-one',status:'failed',steps:[]});
    api.runWorkflowGraph.mockRejectedValue(new Error('Pinned history missing'));
    const view=render(StateRun,{params:{runId:'run-old'}});
    expect(await view.findByText('Pinned history missing')).toBeTruthy();
    expect(api.pipelineGraph).not.toHaveBeenCalled();
  });
  it('does not silently resolve a project alias as an exact run', async () => {
    api.getRunDetail.mockResolvedValue({id:'some-other-run',project_id:'alias'});
    const view=render(StateRun,{params:{runId:'alias'}});
    expect(await view.findByRole('alert')).toHaveProperty('textContent',expect.stringContaining('exact run ID'));
    expect(api.runWorkflowGraph).not.toHaveBeenCalled();
  });
  it('historical references render separate links and never masquerade as ownership', async () => {
    api.stateRunOwners.mockResolvedValue({links:[
      {project_id:'game',title:'武虾传奇',node_key:'growth.progress',relation:'reference',reference_id:'one'},
      {project_id:'game',title:'武虾传奇',node_key:'growth.progress',relation:'reference',reference_id:'two'}]});
    const view=render(RunStateLinks,{runId:'legacy'});
    await waitFor(()=>expect(view.container.querySelectorAll('a')).toHaveLength(2));
    expect(view.getAllByText('Reference only · not adopted')).toHaveLength(2);
    authStore.set({canWrite:false,permissionResolved:true,email:null});
    await waitFor(()=>expect(view.container.querySelectorAll('a')).toHaveLength(0));
  });
});


describe('Visible node status content', () => {
  it('keeps running attempt separate from the unverified fact', () => {
    const view=render(StateGraph,{nodes:[goal('active',[],{status:'OPEN',readiness:'in_progress',latest_attempt:attempt({status:'running'})})],selected:'',onselect:vi.fn()});
    const card=view.container.querySelector('g.goal')!;
    expect(card.querySelector('.goal-state')?.textContent).toBe('OPEN');
    expect(card.querySelector('.goal-attempt')?.textContent).toContain('RUNNING');
    expect(card.querySelector('.goal-ready')?.textContent).toContain('In progress');
    expect(card.getAttribute('data-fact')).toBe('OPEN');
    expect(card.classList.contains('verified')).toBe(false);
    expect(view.container.querySelector('svg')?.style.width).toMatch(/px$/);
  });
  it('shows accepted fact and the absence of an attempt explicitly', () => {
    const view=render(StateGraph,{nodes:[goal('accepted',[],{status:'VERIFIED',readiness:'closed'})],selected:'',onselect:vi.fn()});
    expect(view.container.querySelector('.goal-state')?.textContent).toBe('VERIFIED');
    expect(view.container.querySelector('.goal-attempt')?.textContent).toContain('Not started');
  });
});


describe('External harness attempts are first-class',()=>{
  it('shows harness and job provenance without a missing-run error or workflow link',async()=>{
    const external=attempt({execution_kind:'external',workflow:null,run_id:null,execution_project_id:null,
      harness:'director-subagents',external_id:'session/worker-7',reporting_actor:'trusted-caller',status:'candidate'} as never);
    api.stateNode.mockResolvedValue({...detail('growth.progress'),attempts:[external]});
    api.stateAttemptDetail.mockResolvedValue({attempt:{...external,context:{}},evidence:[],receipts:[{provenance_json:'{"execution_kind":"external"}'}],
      external_observations:[{observation_id:'finished',version:1,status:'candidate',quiescent:1,report_ref:'reports/final.json',report_sha256:'a'.repeat(64),actor:'trusted-caller',context_hash:'b'.repeat(64),detail:'All verifier workers completed'}],evidence_truncated:false});
    const view=render(StateNodePanel,{projectId:'game',nodeKey:'growth.progress',onselect:vi.fn()});
    await view.findByText('director-subagents');await fireEvent.click(view.getByRole('button',{name:'Inspect evidence'}));
    await view.findByText('Own harness: no SkillFlow Run is required.');
    expect(view.container.querySelector('a[href^="#/state-runs/"]')).toBeNull();
    expect(view.container.textContent).toContain('session/worker-7');
    expect(view.container.textContent).toContain('Authenticated reporter');
    expect(view.container.querySelector('.external-observation')?.textContent).toContain('CANDIDATE');
    expect(view.container.querySelector('.receipt-provenance')).not.toBeNull();
    expect(api.getRunDetail).not.toHaveBeenCalled();expect(api.runWorkflowGraph).not.toHaveBeenCalled();
  });
  it('distinguishes external progress directly on a graph card',()=>{
    const external=attempt({execution_kind:'external',workflow:null,run_id:null,harness:'CI',status:'running'} as never);
    const view=render(StateGraph,{nodes:[goal('external',[],{latest_attempt:external,status:'OPEN',readiness:'in_progress'})],selected:'',onselect:vi.fn()});
    expect(view.container.querySelector('.goal-attempt')?.textContent).toContain('External: RUNNING');
    expect(view.container.querySelector('.goal-state')?.textContent).toBe('OPEN');
    expect(view.container.querySelector('g.verified')).toBeNull();
  });
  it('project attempt tab retains external executions without inventing a Run',async()=>{
    api.stateAttempts.mockResolvedValue({attempts:[attempt({execution_kind:'external',workflow:null,run_id:null,
      harness:'CI-checker',external_id:'job/321'} as never)],next_after:null});
    const view=render(StateProject,{params:{id:'game'}});await view.findByRole('heading',{name:'武虾传奇'});
    await fireEvent.click(view.getByRole('button',{name:'Runs / attempts'}));
    await view.findByText('CI-checker');expect(view.container.textContent).toContain('job/321');
    expect(view.container.querySelector('.run-card a[href^="#/state-runs/"]')).toBeNull();
  });
});
