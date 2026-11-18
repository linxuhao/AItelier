import { beforeEach,afterEach,describe,it,expect,vi } from 'vitest';
import { render,cleanup,fireEvent,waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { overview,detail,runSummary } from '../fixtures/stateProject';
const api=vi.hoisted(()=>({driverStatus:vi.fn(),dshDriverConversationUnread:vi.fn(),dshDriverConversation:vi.fn(),dshDriverHistory:vi.fn(),sendDriverConversationMessage:vi.fn(),markDriverConversationRead:vi.fn(),messageDshDriver:vi.fn(),startDshDriver:vi.fn(),stopDshDriver:vi.fn(),stateProjects:vi.fn(),stateProjectArchive:vi.fn(),stateOverview:vi.fn(),stateRunSummary:vi.fn(),stateNode:vi.fn(),stateAttempts:vi.fn(),stateDriverNote:vi.fn(),stateRefreshProject:vi.fn(),stateAttemptDetail:vi.fn(),
  runHistory:vi.fn(),listPipelines:vi.fn(),pipelineGraph:vi.fn(),pipelineStateFile:vi.fn(),getTrace:vi.fn(),setUserLang:vi.fn(),
  listRepos:vi.fn(),listAllRuns:vi.fn(),createProject:vi.fn(),deleteProject:vi.fn()}));
vi.mock('../../lib/api',()=>api);
import StateProjects from '../../views/StateProjects.svelte';
import RelatedStateProjects from '../../views/RelatedStateProjects.svelte';
import ProjectDashboard from '../../views/ProjectDashboard.svelte';

const row=(id:string,dispatch='active',archived?:boolean)=>({project_id:id,title:id,source_project_id:null,node_count:1,verified_count:1,source:{repo_path:null},policy:{dispatch,revision:0,reason:''},...(archived===undefined?{}:{archived})});
/** One row as the server answers it on a page that ASKED for archived rows: the
 *  archive fact is a boolean on EVERY row, `false` included. */
const archivedRow=(id:string)=>({...row(id,'active',true)});
/** The rows the page actually put on screen: one project card each. */
const renderedCards=(view:{container:HTMLElement})=>view.container.querySelectorAll('.project-card:not(.steward-card)').length;
/** The hidden count the page displays, read off the element that renders it. */
const hiddenCount=(view:{container:HTMLElement})=>{
  const label=view.container.querySelector('[data-archived-hidden]');
  expect(label).toBeTruthy();
  return Number(label!.getAttribute('data-archived-hidden'));
};
/** The catalog answer the SERVER gives: the archived row only exists in the
 *  answer to a request that asked for archived rows. */
function catalog(all:any[]){
  api.stateProjects.mockImplementation(async (_repo?:string,_after='',includeArchived=false)=>
    ({projects:includeArchived?all:all.filter(p=>p.archived!==true),next_after:null}));
  return all;
}
let storage:Map<string,string>;
beforeEach(()=>{
  cleanup();vi.resetAllMocks();api.driverStatus.mockResolvedValue({total:0,online:0,projects:{}});
  api.dshDriverConversationUnread.mockResolvedValue({projects:{},total:0});
  api.dshDriverConversation.mockResolvedValue({project_id:'live',messages:[],has_more:false,next_cursor:null,end_of_history:true,matched_total:0,unread_count:0,read_upto_message_id:null,read_upto_created_at:null});
  api.dshDriverHistory.mockResolvedValue({driver_id:'dsh',session_id:'s',entries:[],has_more:false,next_before_seq:null});
  api.sendDriverConversationMessage.mockResolvedValue({});api.markDriverConversationRead.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});api.startDshDriver.mockResolvedValue({});api.stopDshDriver.mockResolvedValue({});
  api.stateProjectArchive.mockResolvedValue({archived:true,changed:true});
  langStore.set('en');storage=new Map();
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
    await fireEvent.input(view.getByRole('searchbox'),{target:{value:'canary'}});
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

describe('the archive fact the server returned (ticket catalog-reports-which-rows-are-archived)',()=>{
  it('asks for no archived rows while the toggle is off, and labels nothing as archived',async()=>{
    const answer=catalog([row('live','active',false),row('paused','hold',false),archivedRow('old-canary')]);
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    // the request the page ACTUALLY made: no include_archived on the wire
    expect(api.stateProjects.mock.calls.filter(call=>call[2]===true)).toHaveLength(0);
    expect(view.queryByRole('heading',{name:'old-canary'})).toBeNull();
    expect(view.container.querySelector('[data-archived="true"]')).toBeNull();
    // the count is what the server returned minus what the page shows
    expect(hiddenCount(view)).toBe(answer.filter(p=>p.archived!==true).length-renderedCards(view));
    expect(hiddenCount(view)).toBe(0);
  });
  it('asks the server for archived rows once when the toggle is on, and renders them with the fact',async()=>{
    const answer=catalog([row('live','active',false),row('paused','hold',false),archivedRow('old-canary')]);
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    await fireEvent.click(view.getByLabelText(/Show archived/));
    await view.findByRole('heading',{name:'old-canary'});
    expect(api.stateProjects.mock.calls.filter(call=>call[2]===true)).toHaveLength(1);
    // the archived row is drawn with the fact on it, and only that row
    expect(view.container.querySelectorAll('[data-archived="true"]')).toHaveLength(1);
    expect(view.container.querySelector('[data-archived="true"]')!.textContent).toContain('old-canary');
    // every returned row is on screen, so nothing is hidden: the count is the
    // response length minus the rendered cards, both measured here
    expect(renderedCards(view)).toBe(answer.length);
    expect(hiddenCount(view)).toBe(answer.length-renderedCards(view));
    expect(hiddenCount(view)).toBe(0);
  });
  it('labels the hidden rows from the answer the server gave, not from a constant',async()=>{
    // A server that has NOT learned to filter (the pre-archive builds) still
    // returns the row the dispatch policy hides; whatever it returns, the number
    // on screen is that answer minus the cards drawn from it.
    const answer={projects:[row('live'),row('old-canary','archive'),row('paused','hold')],next_after:null};
    api.stateProjects.mockResolvedValue(answer);
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    expect(api.stateProjects.mock.calls.filter(call=>call[2]===true)).toHaveLength(0);
    expect(view.container.querySelectorAll('[data-archived="true"]')).toHaveLength(0);
    expect(renderedCards(view)).toBe(2);
    expect(hiddenCount(view)).toBe(answer.projects.length-renderedCards(view));
    expect(hiddenCount(view)).toBe(1);
    // ...and it is not the archived-flag count either: with the toggle on, the
    // archived row IS shown, so the count stays 0 although the answer carries
    // exactly one archived fact
    cleanup();api.stateProjects.mockResolvedValue({projects:[row('live','active',false),archivedRow('old-canary')],next_after:null});
    const flagged=render(StateProjects);await flagged.findByRole('heading',{name:'live'});
    await fireEvent.click(flagged.getByLabelText(/Show archived/));
    await flagged.findByRole('heading',{name:'old-canary'});
    expect(flagged.container.querySelectorAll('[data-archived="true"]')).toHaveLength(1);
    expect(renderedCards(flagged)).toBe(2);
    expect(hiddenCount(flagged)).toBe(2-renderedCards(flagged));
    expect(hiddenCount(flagged)).toBe(0);
  });
});

describe('archiving and restoring from a project card (ticket owner-can-archive-and-restore-from-the-card)',()=>{
  it('issues the State archive write for the project, then the unarchive write for the hidden one',async()=>{
    catalog([row('live','active',false),row('paused','hold',false),archivedRow('old-canary')]);
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    // Archiving is two taps (the owner asked for a confirmation): the first one
    // stages the question and writes nothing, the second one writes.
    await fireEvent.click(view.getByRole('button',{name:'Archive live'}));
    expect(api.stateProjectArchive).not.toHaveBeenCalled();
    await fireEvent.click(view.getByRole('button',{name:'Yes, archive live'}));
    await waitFor(()=>expect(api.stateProjectArchive).toHaveBeenCalledTimes(1));
    expect(api.stateProjectArchive).toHaveBeenCalledWith('live',true);
    await fireEvent.click(view.getByLabelText(/Show archived/));
    await view.findByRole('heading',{name:'old-canary'});
    // Restoring stays ONE tap: un-hiding is reversible.
    await fireEvent.click(view.getByRole('button',{name:'Restore old-canary'}));
    await waitFor(()=>expect(api.stateProjectArchive).toHaveBeenCalledWith('old-canary',false));
    expect(api.stateProjectArchive.mock.calls).toEqual([['live',true],['old-canary',false]]);
  });
  it('shows the server refusal and keeps the project listed, never a silent no-op',async()=>{
    catalog([row('live','active',false),row('paused','hold',false),archivedRow('old-canary')]);
    api.stateProjectArchive.mockRejectedValue(new Error('only an authenticated admin may archive_project'));
    const view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    const before=renderedCards(view);
    await fireEvent.click(view.getByRole('button',{name:'Archive live'}));
    await fireEvent.click(view.getByRole('button',{name:'Yes, archive live'}));
    await waitFor(()=>expect(view.getByRole('alert').textContent).toContain('only an authenticated admin may archive_project'));
    // the refusal is the server's own sentence, and nothing was rewritten: the
    // project is still listed and the page did not pretend it had been archived
    expect(view.getByRole('heading',{name:'live'})).toBeTruthy();
    expect(renderedCards(view)).toBe(before);
    expect(view.container.querySelector('[data-archived="true"]')).toBeNull();
    expect(api.stateProjects).toHaveBeenCalledTimes(1);
  });
  it('carries the request the client actually sends: include_archived in the catalog read, the State command path for the write',async()=>{
    const urls:string[]=[],bodies:string[]=[];
    vi.stubGlobal('fetch',vi.fn(async (url:string,init?:{body?:string})=>{
      urls.push(String(url));bodies.push(String(init?.body??''));
      return new Response(JSON.stringify({projects:[],next_after:null}),{status:200,headers:{'Content-Type':'application/json'}});
    }));
    // the REAL client module, not the mock above: this is the bytes that leave
    const real=await vi.importActual<typeof import('../../lib/api')>('../../lib/api');
    await real.stateProjects();
    await real.stateProjects(undefined,'',true);
    expect(urls[0]).not.toContain('include_archived');
    expect(urls[1]).toContain('include_archived=true');
    await real.stateProjectArchive('live',true);
    await real.stateProjectArchive('old-canary',false);
    expect(urls[2]).toBe('/api/state/commands/archive_project');
    expect(JSON.parse(bodies[2])).toEqual({project_id:'live'});
    expect(urls[3]).toBe('/api/state/commands/unarchive_project');
    expect(JSON.parse(bodies[3])).toEqual({project_id:'old-canary'});
  });
});

describe('new project entry (plugin step 10)',()=>{
  it('offers "New project" to a writer on the home dashboard and the project list',async()=>{
    let view=render(ProjectDashboard);await view.findByRole('heading',{name:'live'});
    expect(view.getByRole('button',{name:'New project'})).toBeTruthy();
    cleanup();
    view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    expect(view.getByRole('button',{name:'New project'})).toBeTruthy();
  });
  it('does not offer it to a reader who cannot write',async()=>{
    authStore.set({canWrite:false,permissionResolved:true,email:null});
    let view=render(ProjectDashboard);await view.findByRole('heading',{name:'live'});
    expect(view.queryByRole('button',{name:'New project'})).toBeNull();
    cleanup();
    view=render(StateProjects);await view.findByRole('heading',{name:'live'});
    expect(view.queryByRole('button',{name:'New project'})).toBeNull();
  });
});
