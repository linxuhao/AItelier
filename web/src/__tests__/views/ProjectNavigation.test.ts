import { beforeEach,afterEach,describe,it,expect,vi } from 'vitest';
import { render,cleanup,waitFor,fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { overview,detail } from '../fixtures/stateProject';
const api=vi.hoisted(()=>({stateProjects:vi.fn(),stateOverview:vi.fn(),stateNode:vi.fn(),stateAttempts:vi.fn(),stateRefreshProject:vi.fn(),stateAttemptDetail:vi.fn(),
  runHistory:vi.fn(),listPipelines:vi.fn(),pipelineGraph:vi.fn(),pipelineStateFile:vi.fn(),getTrace:vi.fn(),setUserLang:vi.fn(),
  listRepos:vi.fn(),listAllRuns:vi.fn(),createProject:vi.fn(),deleteProject:vi.fn()}));
vi.mock('../../lib/api',()=>api);
import ProjectDashboard from '../../views/ProjectDashboard.svelte';
import Runs from '../../views/Runs.svelte';
import Pipelines from '../../views/Pipelines.svelte';
import Repositories from '../../views/Repositories.svelte';
import { navArea } from '../../lib/navigation.svelte';

const row=(id='game')=>({project_id:id,title:id==='game'?'武虾传奇':id,source_project_id:null,node_count:2,verified_count:0,source:{repo_path:null},policy:{dispatch:'active'}});
const run=(id='run-1',patch={})=>({id,project_id:'exec-1',execution_name:'Example',config_name:'feature',status:'running',graph_version:3,created_at:'2026-09-08',updated_at:'2026-09-08',state_project_id:null,state_node_key:null,...patch});
let storage:Map<string,string>;
beforeEach(()=>{
  cleanup();vi.resetAllMocks();langStore.set('en');storage=new Map();
  vi.stubGlobal('localStorage',{getItem:(k:string)=>storage.get(k)??null,setItem:(k:string,v:string)=>storage.set(k,v)});
  authStore.set({canWrite:true,permissionResolved:true,email:'owner@local'});
  api.stateProjects.mockResolvedValue({projects:[row()],next_after:null});
  api.stateOverview.mockImplementation(async(id:string)=>({...overview(),project:{...overview().project,project_id:id,title:id==='game'?'武虾传奇':id}}));
  api.stateNode.mockImplementation(async(_p,k)=>detail(k));api.stateAttempts.mockResolvedValue({attempts:[],next_after:null});
  api.runHistory.mockResolvedValue({runs:[run()],total:1,next_offset:null});
  api.listPipelines.mockResolvedValue({pipelines:[{config_name:'gen_report',label:'Generated report',origin:'generated',step_count:1,state_files:[{name:'notes.md',size:20}]}]});
  api.pipelineGraph.mockResolvedValue({begin:'work',steps:[{id:'work',type:'agent',transitions:[]}]});
  api.pipelineStateFile.mockResolvedValue({content:'Retained workflow notes',truncated:false});
  api.listRepos.mockResolvedValue([]);api.listAllRuns.mockResolvedValue({runs:[]});
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.useRealTimers();});

describe('Project-first homepage',()=>{
  it('opens a real state graph directly, not just a project card list',async()=>{
    const view=render(ProjectDashboard);await view.findByRole('heading',{name:'武虾传奇'});
    expect(view.container.querySelectorAll('g.goal')).toHaveLength(2);
    expect(api.listAllRuns).not.toHaveBeenCalled();expect(api.listPipelines).not.toHaveBeenCalled();
    expect(api.stateRefreshProject).not.toHaveBeenCalled();
  });
  it('restores a permitted selection and lets the user switch without mixing data',async()=>{
    storage.set('aitelier_last_state_project','other');api.stateProjects.mockResolvedValue({projects:[row(),row('other')],next_after:null});
    const view=render(ProjectDashboard);await view.findByRole('heading',{name:'other'});
    await fireEvent.change(view.getByLabelText('Current project'),{target:{value:'game'}});
    await view.findByRole('heading',{name:'武虾传奇'});expect(storage.get('aitelier_last_state_project')).toBe('game');
  });
  it('does not load a remembered project absent from authorized results',async()=>{
    storage.set('aitelier_last_state_project','someone-elses-project');render(ProjectDashboard);
    await waitFor(()=>expect(api.stateOverview).toHaveBeenCalledWith('game'));
    expect(api.stateOverview).not.toHaveBeenCalledWith('someone-elses-project');
  });
  it('is private while signed out and erases previously loaded goals after signout',async()=>{
    const view=render(ProjectDashboard);await view.findByRole('heading',{name:'武虾传奇'});
    authStore.set({canWrite:false,permissionResolved:true,email:null});
    await view.findByText(/Project state is private/);
    expect(view.container.querySelectorAll('g.goal')).toHaveLength(0);
    expect(api.stateProjects).toHaveBeenCalledTimes(1);
  });
  it('handles no projects and a failed fetch without pretending either is a loaded graph',async()=>{
    api.stateProjects.mockRejectedValueOnce(new Error('catalog unavailable'));
    const view=render(ProjectDashboard);await view.findByRole('alert');
    api.stateProjects.mockResolvedValue({projects:[],next_after:null});await fireEvent.click(view.getByText('Retry'));
    await view.findByText(/No state projects yet/);expect(view.container.querySelectorAll('g.goal')).toHaveLength(0);
  });
  it('rejects late results from a previous signed-in identity',async()=>{
    let resolve:(r:any)=>void=()=>{};api.stateProjects.mockReturnValueOnce(new Promise(r=>resolve=r));
    const view=render(ProjectDashboard);authStore.set({canWrite:false,permissionResolved:true,email:null});
    await view.findByText(/Project state is private/);resolve({projects:[row('private-old')],next_after:null});
    await Promise.resolve();expect(api.stateOverview).not.toHaveBeenCalled();
  });
});

describe('Runs and definitions have separate queries',()=>{
  it('shows exact run identities, state owners and repo-less executions',async()=>{
    api.runHistory.mockResolvedValue({runs:[run('r1',{state_project_id:'game',state_node_key:'growth.progress'}),run('r2',{config_name:'pipeline_forge'})],total:2,next_offset:null});
    const view=render(Runs);await view.findByText('pipeline_forge');
    expect(view.container.querySelector('a[href="#/state-runs/r1"]')).toBeTruthy();
    expect(view.container.querySelector('a[href="#/state-projects/game/nodes/growth.progress"]')).toBeTruthy();
    expect(api.listAllRuns).not.toHaveBeenCalled();expect(api.listPipelines).not.toHaveBeenCalled();
  });
  it('filters through the actual run endpoint before paging',async()=>{
    const view=render(Runs,{params:{projectId:'game'}});await view.findByText('RUNNING');
    await fireEvent.change(view.getByLabelText('Run status'),{target:{value:'paused'}});
    await fireEvent.input(view.getByLabelText('Workflow'),{target:{value:'investigate'}});
    await fireEvent.submit(view.container.querySelector('form')!);
    await waitFor(()=>expect(api.runHistory).toHaveBeenLastCalledWith({q:'',status:'paused',workflow:'investigate',state_project_id:'game'}));
  });
  it('loads another page without duplicating run IDs',async()=>{
    api.runHistory.mockResolvedValueOnce({runs:[run('r1')],total:3,next_offset:1}).mockResolvedValueOnce({runs:[run('r1'),run('r2')],total:3,next_offset:null});
    const view=render(Runs);await fireEvent.click(await view.findByText('Load more'));
    await waitFor(()=>expect(view.container.querySelectorAll('.history-run')).toHaveLength(2));
  });
  it('does not request private run history anonymously',async()=>{
    authStore.set({canWrite:false,permissionResolved:true,email:null});const view=render(Runs);
    expect(view.getByText(/Project state is private/)).toBeTruthy();await Promise.resolve();expect(api.runHistory).not.toHaveBeenCalled();
  });
  it('shows run errors explicitly instead of a successful empty list',async()=>{
    api.runHistory.mockRejectedValue(new Error('history unavailable'));const view=render(Runs);
    expect((await view.findByRole('alert')).textContent).toContain('history unavailable');expect(view.queryByText('No matching runs.')).toBeNull();
  });
  it('loads only definitions, fetches graphs/notes on demand, retains notes',async()=>{
    const view=render(Pipelines);await view.findByRole('heading',{name:'Generated report'});
    expect(api.runHistory).not.toHaveBeenCalled();expect(api.listAllRuns).not.toHaveBeenCalled();expect(api.pipelineGraph).not.toHaveBeenCalled();
    const graphDetails=view.container.querySelector('.pipeline-card details') as HTMLDetailsElement;
    graphDetails.open=true;await fireEvent(graphDetails,new Event('toggle'));
    await waitFor(()=>expect(api.pipelineGraph).toHaveBeenCalledWith('gen_report'));
    await fireEvent.click(view.getByRole('button',{name:/notes.md/}));await view.findByText('Retained workflow notes');
  });
  it('failed notes can be retried, not cached as success',async()=>{
    api.pipelineStateFile.mockRejectedValueOnce(new Error('note unavailable')).mockResolvedValueOnce({content:'Recovered note'});
    const view=render(Pipelines);await view.findByRole('heading',{name:'Generated report'});
    const button=view.getByRole('button',{name:/notes.md/});await fireEvent.click(button);await view.findByRole('alert');
    await fireEvent.click(button);await fireEvent.click(button);await view.findByText('Recovered note');
  });
  it('repository tools no longer load the run feed or definition catalog',async()=>{
    render(Repositories);await waitFor(()=>expect(api.listRepos).toHaveBeenCalled());
    expect(api.listAllRuns).not.toHaveBeenCalled();expect(api.listPipelines).not.toHaveBeenCalled();
  });
  it('links default project routes and old execution routes to the right nav item',()=>{
    for(const route of ['#/','#/projects','#/state-projects/game'])expect(navArea(route)).toBe('projects');
    for(const route of ['#/runs','#/state-runs/r1','#/projects/old-execution','#/repos'])expect(navArea(route)).toBe('runs');
    expect(navArea('#/pipelines')).toBe('pipelines');
  });
});
