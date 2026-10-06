import { beforeEach,afterEach,describe,it,expect,vi } from 'vitest';
import { render,cleanup,fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { overview,detail,runSummary } from '../fixtures/stateProject';
const api=vi.hoisted(()=>({stateProjects:vi.fn(),stateOverview:vi.fn(),stateRunSummary:vi.fn(),stateNode:vi.fn(),stateAttempts:vi.fn(),stateDriverNote:vi.fn(),stateRefreshProject:vi.fn(),stateAttemptDetail:vi.fn(),
  runHistory:vi.fn(),listPipelines:vi.fn(),pipelineGraph:vi.fn(),pipelineStateFile:vi.fn(),getTrace:vi.fn(),setUserLang:vi.fn(),
  listRepos:vi.fn(),listAllRuns:vi.fn(),createProject:vi.fn(),deleteProject:vi.fn()}));
vi.mock('../../lib/api',()=>api);
import StateProjects from '../../views/StateProjects.svelte';
import RelatedStateProjects from '../../views/RelatedStateProjects.svelte';
import ProjectDashboard from '../../views/ProjectDashboard.svelte';

const row=(id:string,dispatch='active')=>({project_id:id,title:id,source_project_id:null,node_count:1,verified_count:1,source:{repo_path:null},policy:{dispatch,revision:0,reason:''}});
let storage:Map<string,string>;
beforeEach(()=>{
  cleanup();vi.resetAllMocks();langStore.set('en');storage=new Map();
  vi.stubGlobal('localStorage',{getItem:(k:string)=>storage.get(k)??null,setItem:(k:string,v:string)=>storage.set(k,v)});
  authStore.set({canWrite:true,permissionResolved:true,email:'owner@local'});
  api.stateProjects.mockResolvedValue({projects:[row('live'),row('old-canary','archive'),row('paused','hold')],next_after:null});
  api.stateOverview.mockImplementation(async(id:string)=>({...overview(),project:{...overview().project,project_id:id,title:id}}));
  api.stateRunSummary.mockImplementation(async(id:string)=>({...runSummary(),project_id:id}));
  api.stateNode.mockImplementation(async(_p,k)=>detail(k));api.stateAttempts.mockResolvedValue({attempts:[],next_after:null});
  api.stateDriverNote.mockResolvedValue({project_id:'live',revision:1,index:[],entry_count:0,listed_count:0,delisted_count:0});
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});

describe('archived state projects',()=>{
  it('hides archived projects from the list by default, keeps held ones, and says how many are hidden',async()=>{
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    expect(view.queryByRole('heading',{name:'old-canary'})).toBeNull();
    expect(view.getByRole('heading',{name:'paused'})).toBeTruthy();
    expect(view.getByText(/Archived \(hidden\): 1/)).toBeTruthy();
  });
  it('shows them again when asked, and searching does not resurrect hidden ones',async()=>{
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    await fireEvent.input(view.getByPlaceholderText('Find a goal'),{target:{value:'canary'}});
    expect(view.queryByRole('heading',{name:'old-canary'})).toBeNull();
    await fireEvent.click(view.getByLabelText(/Show archived/));
    await view.findByRole('heading',{name:'old-canary'});
    expect(view.queryByText(/Archived \(hidden\)/)).toBeNull();
  });
  it('does not list archived projects as related state projects',async()=>{
    api.stateProjects.mockResolvedValue({projects:[row('old-canary','archive'),row('live')],next_after:null});
    const view=render(RelatedStateProjects,{repoPath:'/repo'});
    await view.findByText(/live · active/);
    expect(view.container.textContent).not.toContain('old-canary');
  });
  it('keeps archived projects out of the home picker and ignores a remembered archived selection',async()=>{
    storage.set('aitelier_last_state_project','old-canary');
    const view=render(ProjectDashboard);await view.findByRole('heading',{name:'live'});
    const picker=view.getByLabelText('Current project') as HTMLSelectElement;
    const options=[...picker.options].map(o=>o.value);
    expect(options).toEqual(['live','paused']);
    expect(api.stateOverview).not.toHaveBeenCalledWith('old-canary');
  });
});
