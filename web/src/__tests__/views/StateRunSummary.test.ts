import {beforeEach,afterEach,describe,it,expect,vi} from 'vitest';
import {render,cleanup,fireEvent,waitFor} from '@testing-library/svelte';
import {authStore} from '../../stores/auth';
import {langStore} from '../../stores/i18n';
import {runSummary} from '../fixtures/stateProject';
const api=vi.hoisted(()=>({stateRunSummary:vi.fn()}));
vi.mock('../../lib/api',()=>api);
import StateRunSummary from '../../views/StateRunSummary.svelte';

function active(){
  return {...runSummary(),counts:{total:9,running:1,finished:5,failed:2,other:1,unavailable:0},
    execution_counts:{total:9,running:1,finished:5,failed:2,other:1,unavailable:0},
    running_runs:[{run_id:'run-active-exact-id',workflow:'feature_delivery',node_keys:['growth.progress','month.actions'],
      status:'running',current_node:'implement',started_at:'2026-09-08T17:00:00Z'}],
    usage:{...runSummary().usage,total_tokens:4000,prompt_tokens:3400,completion_tokens:600,
      cache_hit_tokens:1400,cache_miss_tokens:1600,cache_hit_ratio:1400/3000,cache_covered_tokens:3000,
      usage_turns:10,token_reported_turns:10,cache_reported_turns:8,runs_with_token_usage:8,runs_without_token_usage:1,partial:true}};
}

beforeEach(()=>{vi.resetAllMocks();langStore.set('en');authStore.set({canWrite:true,permissionResolved:true,email:'owner@test'});api.stateRunSummary.mockResolvedValue(active());});
afterEach(()=>{cleanup();});

describe('State graph run summary',()=>{
  it('renders exactly six non-interactive counters and links only running rows',async()=>{
    const view=render(StateRunSummary,{projectId:'game'});
    await view.findByText('feature_delivery');
    const cards=view.container.querySelectorAll('.run-summary-metrics>div');expect(cards).toHaveLength(6);
    expect(view.container.querySelectorAll('.run-summary-metrics a,.run-summary-metrics button,[data-metric][role="button"]')).toHaveLength(0);
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('9');
    expect(view.container.querySelector('[data-metric="running"] dd')?.textContent).toBe('1');
    expect(view.container.querySelector('[data-metric="finished"] dd')?.textContent).toBe('5');
    expect(view.container.querySelector('[data-metric="failed"] dd')?.textContent).toBe('2');
    expect(view.container.querySelector('[data-metric="tokens"] dd')?.textContent).toBe('4K*');
    expect(view.container.querySelector('[data-metric="cache"] dd')?.textContent).toBe('46.7%');
    expect(view.container.querySelector('[data-metric="cache"] small')?.textContent).toContain('1.4K');
    const links=view.container.querySelectorAll('a');expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe('#/state-runs/run-active-exact-id');
    expect(view.container.querySelector('[data-metric="finished"]')?.getAttribute('title')).toContain('not State VERIFIED');
    expect(view.container.textContent).toContain('8/9');
  });
  it('updates counters and removes a finished run from the running-only list',async()=>{
    const view=render(StateRunSummary,{projectId:'game',refresh:0});await view.findByText('feature_delivery');
    const result=active();result.running_runs=[];result.counts.running=0;result.counts.finished=6;
    result.execution_counts.running=0;result.execution_counts.finished=6;
    api.stateRunSummary.mockResolvedValue(result);
    await view.rerender({projectId:'game',refresh:1});await view.findByText('No related run is currently running.');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
    expect(view.container.querySelector('[data-metric="finished"] dd')?.textContent).toBe('6');
    expect(api.stateRunSummary).toHaveBeenCalledTimes(2);
  });
  it('does not turn missing usage into zero tokens or a zero cache ratio',async()=>{
    api.stateRunSummary.mockResolvedValue({...runSummary(),counts:{...runSummary().counts,total:1,running:1},
      execution_counts:{...runSummary().execution_counts,total:1,running:1},
      usage:{...runSummary().usage,total_tokens:null,runs_without_token_usage:1,partial:true}});
    const view=render(StateRunSummary,{projectId:'game'});await waitFor(()=>expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('1'));
    expect(view.container.querySelector('[data-metric="tokens"] dd')?.textContent).toBe('—');
    expect(view.container.querySelector('[data-metric="cache"] dd')?.textContent).toBe('—');
    expect(view.container.textContent).toContain('Unreported usage is unknown');
  });
  it('has an honest zero-run empty state, with no links',async()=>{
    api.stateRunSummary.mockResolvedValue(runSummary());const view=render(StateRunSummary,{projectId:'game'});
    await view.findByText('No related run is currently running.');
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('0');
    expect(view.container.querySelector('[data-metric="tokens"] dd')?.textContent).toBe('0');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
  });
  it('marks incomplete status observations as lower bounds, not all idle',async()=>{
    api.stateRunSummary.mockResolvedValue({...runSummary(),runtime_unavailable:true,counts:{...runSummary().counts,total:3,unavailable:3},
      execution_counts:{...runSummary().execution_counts,total:3,unavailable:3},
      usage:{...runSummary().usage,total_tokens:null,partial:true,runs_without_token_usage:3}});
    const view=render(StateRunSummary,{projectId:'game'});await view.findByText(/Run statuses unavailable/);
    expect(view.container.querySelector('[data-metric="running"] dd')?.textContent).toBe('≥ 0');
    expect(view.queryByText('No related run is currently running.')).toBeNull();
  });
  it('lists external agents holding a task, as reported, without inventing run links',async()=>{
    api.stateRunSummary.mockResolvedValue({...runSummary(),external_attempts_excluded:3,
      observed_at:'2026-09-09T10:00:00Z',
      external_counts:{active:2,finished:0,failed:0,other:1,total:3},
      execution_counts:{total:3,running:2,finished:0,failed:0,other:1,unavailable:0},
      running_external:[
        {attempt_id:'attempt-reported',node_key:'growth.progress',node_revision:1,harness:'director-subagents',
         external_id:'job-7f3c',reporting_actor:'director@test',status:'running',observation_version:2,
         created_at:'2026-09-09T08:00:00Z',updated_at:'2026-09-09T09:30:00Z',last_report_at:'2026-09-09T09:30:00Z'},
        {attempt_id:'attempt-silent',node_key:'month.actions',node_revision:2,harness:'own-harness',
         external_id:'job-a19b',reporting_actor:'director@test',status:'running',observation_version:0,
         created_at:'2026-09-09T09:00:00Z',updated_at:'2026-09-09T09:00:00Z',last_report_at:null}]});
    const view=render(StateRunSummary,{projectId:'game'});
    await view.findByText('director-subagents');
    const rows=view.container.querySelectorAll('.external-running li');expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('growth.progress');
    expect(rows[0].textContent).toContain('job-7f3c');
    expect(rows[0].textContent).toContain('⏱ 2h 0m');
    expect(rows[0].textContent).toContain('↩ 30m 0s');
    expect(rows[1].textContent).toContain('No report yet');
    // The counters count executions, so a listed agent is never a row under a zero.
    expect(view.container.querySelector('[data-metric="running"] dd')?.textContent).toBe('2');
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('3');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
    expect(view.getByText('No related run is currently running.')).toBeTruthy();
    expect(view.container.textContent).toContain('not observed here');
  });
  it('ages both lists against the snapshot, not the browser clock',async()=>{
    api.stateRunSummary.mockResolvedValue({...active(),observed_at:'2026-09-08T18:00:00Z',
      running_runs:[{...active().running_runs[0],started_at:'2026-09-08T17:12:00Z'}],
      external_counts:{active:1,finished:0,failed:0,other:0,total:1},external_attempts_excluded:1,
      execution_counts:{...active().execution_counts,total:10,running:2},
      running_external:[{attempt_id:'attempt-one',node_key:'growth.progress',node_revision:1,harness:'own-harness',
        external_id:'job-1',reporting_actor:'director@test',status:'running',observation_version:1,
        created_at:'2026-09-08T14:30:00Z',updated_at:'2026-09-08T17:55:00Z',last_report_at:'2026-09-08T17:55:00Z'}]});
    const view=render(StateRunSummary,{projectId:'game'});
    await view.findByText('own-harness');
    expect(view.container.querySelector('.running-runs .elapsed')?.textContent).toContain('48m 0s');
    expect(view.container.querySelector('.external-running .elapsed')?.textContent).toContain('3h 30m');
    // Last report shown as an age too, not a raw timestamp.
    expect(view.container.querySelector('.external-running small')?.textContent).toBe('↩ 5m 0s');
  });
  it('shows no age when the run never reported a start',async()=>{
    api.stateRunSummary.mockResolvedValue({...active(),running_runs:[{...active().running_runs[0],started_at:null}]});
    const view=render(StateRunSummary,{projectId:'game'});
    await view.findByText('feature_delivery');
    expect(view.container.querySelector('.running-runs .elapsed')).toBeNull();
  });
  it('jumps to the goal the external agent is working on',async()=>{
    const jump=vi.fn();
    api.stateRunSummary.mockResolvedValue({...runSummary(),external_attempts_excluded:1,
      external_counts:{active:1,finished:0,failed:0,other:0,total:1},
      execution_counts:{...runSummary().execution_counts,total:1,running:1},
      running_external:[{attempt_id:'attempt-one',node_key:'month.actions',node_revision:1,harness:'own-harness',
        external_id:'job-1',reporting_actor:'director@test',status:'running',observation_version:1,
        created_at:'2026-09-09T08:00:00Z',updated_at:'2026-09-09T08:10:00Z',last_report_at:'2026-09-09T08:10:00Z'}]});
    const view=render(StateRunSummary,{projectId:'game',onselect:jump});
    await fireEvent.click(await view.findByRole('button',{name:'month.actions'}));
    expect(jump).toHaveBeenCalledWith('month.actions');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
  });
  it('drops an external agent that no longer holds the task',async()=>{
    const holding={...runSummary(),external_attempts_excluded:1,
      external_counts:{active:1,finished:0,failed:0,other:0,total:1},
      execution_counts:{...runSummary().execution_counts,total:1,running:1},
      running_external:[{attempt_id:'attempt-one',node_key:'growth.progress',node_revision:1,harness:'own-harness',
        external_id:'job-1',reporting_actor:'director@test',status:'running',observation_version:1,
        created_at:'2026-09-09T08:00:00Z',updated_at:'2026-09-09T08:10:00Z',last_report_at:'2026-09-09T08:10:00Z'}]};
    api.stateRunSummary.mockResolvedValue(holding);
    const view=render(StateRunSummary,{projectId:'game',refresh:0});await view.findByText('own-harness');
    api.stateRunSummary.mockResolvedValue({...holding,external_counts:{active:0,finished:1,failed:0,other:0,total:1},
      execution_counts:{...runSummary().execution_counts,total:1,finished:1},running_external:[]});
    await view.rerender({projectId:'game',refresh:1});
    await waitFor(()=>expect(view.container.querySelectorAll('.external-running li')).toHaveLength(0));
    expect(view.container.querySelector('[data-metric="running"] dd')?.textContent).toBe('0');
    expect(view.container.querySelector('[data-metric="finished"] dd')?.textContent).toBe('1');
  });
  it('explains excluded external harness attempts without creating fake run links',async()=>{
    api.stateRunSummary.mockResolvedValue({...runSummary(),external_attempts_excluded:2,
      external_counts:{active:0,finished:2,failed:0,other:0,total:2},
      execution_counts:{...runSummary().execution_counts,total:2,finished:2}});
    const view=render(StateRunSummary,{projectId:'game'});await view.findByText(/External harness attempts are not workflow runs/);
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('2');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
  });
  it('shows a failed request as unavailable, never a successful empty panel',async()=>{
    api.stateRunSummary.mockRejectedValue(new Error('summary unavailable'));
    const view=render(StateRunSummary,{projectId:'game'});await view.findByRole('alert');
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('—');
    expect(view.queryByText('No related run is currently running.')).toBeNull();
  });
  it('clears stale links after a reload failure',async()=>{
    const view=render(StateRunSummary,{projectId:'game'});await view.findByText('feature_delivery');
    api.stateRunSummary.mockRejectedValue(new Error('permission changed'));
    await view.rerender({projectId:'game',refresh:1});await view.findByRole('alert');
    expect(view.container.querySelectorAll('a')).toHaveLength(0);
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('—');
  });
  it('does not fetch private summaries anonymously and erases on logout',async()=>{
    authStore.set({canWrite:false,permissionResolved:true,email:null});const view=render(StateRunSummary,{projectId:'game'});
    await Promise.resolve();expect(api.stateRunSummary).not.toHaveBeenCalled();
    authStore.set({canWrite:true,permissionResolved:true,email:'owner@test'});await view.findByText('feature_delivery');
    authStore.set({canWrite:false,permissionResolved:true,email:null});
    await waitFor(()=>expect(view.container.querySelector('section')).toBeNull());
  });
  it('ignores a slow response from a previously selected project',async()=>{
    let finish:(value:unknown)=>void=()=>{};
    api.stateRunSummary.mockReturnValueOnce(new Promise(resolve=>finish=resolve));
    const view=render(StateRunSummary,{projectId:'game'});
    api.stateRunSummary.mockResolvedValue({...runSummary(),project_id:'other'});
    await view.rerender({projectId:'other',refresh:1});await view.findByText('No related run is currently running.');
    finish(active());await Promise.resolve();await Promise.resolve();
    expect(view.queryByText('feature_delivery')).toBeNull();
    expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('0');
  });
});


it('rejects a mismatched response rather than showing another project’s counters',async()=>{
  api.stateRunSummary.mockResolvedValue({...active(),project_id:'someone-else'});
  const view=render(StateRunSummary,{projectId:'game'});await view.findByRole('alert');
  expect(view.queryByText('feature_delivery')).toBeNull();
  expect(view.container.querySelector('[data-metric="total"] dd')?.textContent).toBe('—');
});
