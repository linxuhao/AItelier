import type { StateNodeSummary, StateOverview, StateNodeDetail, StateAttempt } from '../../lib/stateGraph';
export function goal(key: string, deps: string[] = [], patch: Partial<StateNodeSummary> = {}): StateNodeSummary {
  return { node_key: key, title: key === 'growth.progress' ? 'Persistent proficiency' : 'Monthly actions',
    domain: key.split('.')[0], revision: 1, contract_hash: 'a'.repeat(64), status: 'OPEN',
    readiness: deps.length ? 'blocked' : 'ready', dependencies: deps, blocked_by: deps,
    hold: null, node_hold: {revision: 0, held: 0, reason: ''}, criteria_count: 1,
    attempt_count: 0, latest_attempt: null, latest_evidence: {}, priority: 0, ...patch };
}
export function attempt(patch: Partial<StateAttempt> = {}): StateAttempt {
  return {seq: 1, attempt_id: 'attempt-one', project_id: 'game', node_key: 'growth.progress',
    node_revision: 1, workflow: 'feature', execution_project_id: 'sg-one', run_id: 'run-one',
    status: 'candidate', artifact_ref: 'b'.repeat(40), created_at: '2026-09-08T08:00:00Z',
    updated_at: '2026-09-08T09:00:00Z', ...patch};
}
export function overview(): StateOverview {
  return {project: {project_id: 'game', title: '武虾传奇', source_project_id: null},
    source: {kind: 'stable_binding', repo_path: '/private/source', common_dir: '/private/source/.git', revision: 1},
    policy: {revision: 1, dispatch: 'hold', reason: 'Migration review; no new dispatch'},
    nodes: [goal('growth.progress', [], {status: 'CANDIDATE', readiness: 'held', latest_attempt: attempt(),
      latest_evidence: {fail: 1}, hold: {scope: 'project', reason: 'Migration review'}}),
      goal('month.actions', ['growth.progress'], {readiness: 'held', hold: {scope: 'project', reason: 'Migration review'}})],
    counts: {CANDIDATE: 1, OPEN: 1}, readiness_counts: {held: 2}, event_seq: 7,
    observed_at: '2026-09-08T09:10:00Z', run_state_mode: 'persisted; explicit refresh'};
}
export function detail(key = 'growth.progress'): StateNodeDetail {
  const n = overview().nodes.find(n => n.node_key === key) ?? goal(key);
  return {node: {...n, goal: n.title + '\nDetailed requirement for ' + key,
    acceptance: [{id: 'save-load', kind: 'test', description: 'Progress survives save and reload'}]},
    dependency_receipts: Object.fromEntries(n.dependencies.map(d => [d, {goal: d, status: 'CANDIDATE', revision: 1, acceptance: null}])),
    attempts: key === 'growth.progress' ? [attempt()] : [], references: {references: [], next_after: null}};
}


export function runSummary() {
  return {project_id:'game',observed_at:'2026-09-08T17:00:00Z',runtime_unavailable:false,external_attempts_excluded:0,
    counts:{total:0,running:0,finished:0,failed:0,other:0,unavailable:0},running_runs:[],
    running_external:[],external_counts:{active:0,total:0},
    usage:{total_tokens:0,prompt_tokens:0,completion_tokens:0,cache_hit_tokens:null,cache_miss_tokens:null,cache_hit_ratio:null,
      cache_covered_tokens:0,usage_turns:0,token_reported_turns:0,cache_reported_turns:0,runs_with_token_usage:0,
      runs_without_token_usage:0,usage_errors:0,partial:false}};
}
