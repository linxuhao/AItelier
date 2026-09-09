/** Domain types/layout for persistent goals. Never fold run completed into VERIFIED. */
import { layoutGraph } from './pipelineLayout';

export interface DispatchPolicy { revision: number; dispatch: 'active' | 'hold' | 'archive'; reason: string }
export interface NodeHold { revision: number; held: number; reason: string }
export interface SourceBinding { kind: string; repo_path: string | null; common_dir?: string | null; revision: number }
export interface StateProjectRow {
  project_id: string; title: string; source_project_id: string | null;
  node_count: number; verified_count: number; source: SourceBinding; policy: DispatchPolicy;
}
export interface StateAttempt {
  seq: number; attempt_id: string; project_id: string; node_key: string; node_revision: number;
  workflow: string | null; execution_project_id: string | null; run_id: string | null; status: string;
  artifact_ref: string | null; created_at: string; updated_at: string; title?: string;
  execution_kind?: 'skillflow' | 'external'; harness?:string|null; external_id?:string|null;
  reporting_actor?:string|null; observation_version?:number; artifact_kind?:string|null;
}
export type StateNextAction = 'candidate_review' | 'new_attempt';
export interface StateNodeSummary {
  node_key: string; title: string; domain: string; revision: number; contract_hash: string; facet?: string | null;
  status: string; readiness: string; next_action?: StateNextAction | null; dependencies: string[]; blocked_by: string[];
  hold: ({ scope: string; reason: string } & Partial<DispatchPolicy>) | null; node_hold: NodeHold;
  criteria_count: number; attempt_count: number; latest_attempt: StateAttempt | null;
  latest_evidence: Record<string, number>; priority: number;
}
export interface StateOverview {
  project: { project_id: string; title: string; source_project_id: string | null };
  source: SourceBinding; policy: DispatchPolicy; nodes: StateNodeSummary[];
  counts: Record<string, number>; readiness_counts: Record<string, number>;
  ready_action_counts?: Partial<Record<StateNextAction, number>>;
  event_seq: number; observed_at: string; run_state_mode: string;
}
export interface HistoryReference {
  reference_id: string; node_key: string; kind: string; ref: string; label: string;
  observed_status: string; protection: number; provenance_actor: string;
  artifact_ref: string | null; report_sha256: string | null;
}
export interface StateEvidence {
  evidence_id: string; criterion_id: string; verdict: string; reviewer: string;
  artifact_ref: string; report_ref: string; report_sha256: string; detail: string;
}
export interface StateNodeDetail {
  node: StateNodeSummary & { goal: string; acceptance: { id: string; kind: string; description: string }[] };
  dependency_receipts: Record<string, { goal: string; revision: number; status: string; acceptance: Record<string, unknown> | null }>;
  attempts: StateAttempt[];
  references: { references: HistoryReference[]; next_after: string | null };
}
export interface AttemptDetail {
  attempt: StateAttempt & { context: Record<string, unknown> }; evidence: StateEvidence[];
  receipts: Record<string, unknown>[]; evidence_truncated: boolean;
  external_observations?: {observation_id:string;version:number;status:string;quiescent:number;
    report_ref:string;report_sha256:string;actor:string;context_hash:string;detail:string}[];
  external_observations_truncated?:boolean;
}
export interface RunOwner { project_id: string; title: string; node_key: string; relation: string; attempt_id?: string; reference_id?: string }

export function stateProjectHref(project: string, node?: string): string {
  return '#/state-projects/' + encodeURIComponent(project) + (node ? '/nodes/' + encodeURIComponent(node) : '');
}
export function exactRunHref(id: string): string { return '#/state-runs/' + encodeURIComponent(id); }
export function shortText(text: string, n = 30): string {
  const chars = Array.from(text); return chars.length > n ? chars.slice(0, n - 1).join('') + '…' : text;
}
export function cardTitle(text: string, budget = 28): string {
  let used = 0, result = '';
  for (const ch of Array.from(text)) {
    const units = ch.codePointAt(0)! > 255 ? 2 : 1;
    if (used + units > budget) return result + '…';
    result += ch; used += units;
  }
  return result;
}
/** Split labels without assuming every Unicode character has Latin width. */
export function cardLines(text: string, budget = 34): string[] {
  const chars = Array.from(text); const lines: string[] = [];
  let line = '', used = 0;
  for (let i = 0; i < chars.length; i++) {
    const ch = chars[i], units = ch.codePointAt(0)! > 255 ? 2 : 1;
    if (used + units > budget) {
      lines.push(line);
      if (lines.length === 2) { lines[1] = Array.from(lines[1]).slice(0, -1).join('') + '…'; return lines; }
      line = ''; used = 0;
    }
    line += ch; used += units;
  }
  if (line || !lines.length) lines.push(line);
  return lines;
}
export const STATE_CARD = { width: 264, height: 156, xGap: 28, yGap: 36 };
/** A goal's lanes are drawn as small cards inside the goal card. */
export const STATE_LANE = { width: 76, height: 54, gap: 8, x: 13, y: 92, headerHeight: 88 };

/**
 * The server projects this from current State facts. Older servers only return
 * status/readiness, so retain the same conservative classification during a
 * rolling UI deployment. Unknown ready statuses never become launchable here.
 */
export function stateNextAction(node: StateNodeSummary): StateNextAction | null {
  if (node.next_action === 'candidate_review' || node.next_action === 'new_attempt') return node.next_action;
  if (node.readiness !== 'ready') return null;
  if (node.status === 'CANDIDATE') return 'candidate_review';
  return node.status === 'OPEN' || node.status === 'STALE' ? 'new_attempt' : null;
}

export function stateReadyActionCounts(nodes: StateNodeSummary[],
  reported?: Partial<Record<StateNextAction, number>>): Record<StateNextAction, number> {
  const fallback = { candidate_review: 0, new_attempt: 0 };
  for (const node of nodes) {
    const action = stateNextAction(node);
    if (action) fallback[action]++;
  }
  for (const action of ['candidate_review', 'new_attempt'] as const) {
    const count = reported?.[action];
    if (Number.isSafeInteger(count) && count! >= 0) fallback[action] = count!;
  }
  return fallback;
}

export function stateTone(status: string): string {
  // Only the fact is allowed to be green. A successful attempt remains blue.
  return status === 'VERIFIED' ? 'verified' : status === 'CANDIDATE' ? 'candidate'
    : status === 'STALE' ? 'stale' : status === 'SUPERSEDED' ? 'superseded' : 'open';
}

/** One goal's three lanes. `present: false` means that lane has no node yet. */
export interface StateLane { facet:'contract'|'test'|'content'; node_key:string; status:string; readiness:string;
  revision:number; present:boolean }
export interface StateGroup extends StateNodeSummary { lanes:StateLane[]; members:string[] }
const LANE_ORDER:StateLane['facet'][] = ['contract','test','content'];

/** The goal a node belongs to: `x.contract` and `x.test` both belong to `x`. */
export function stemOf(node:{node_key:string; facet?:string|null}):string {
  const { node_key: k, facet } = node;
  if (facet === 'contract' && k.endsWith('.contract')) return k.slice(0, -'.contract'.length);
  if (facet === 'test' && k.endsWith('.test')) return k.slice(0, -'.test'.length);
  return k;
}

/**
 * Collapse each goal's contract / test / implementation into one node.
 *
 * Faceting tripled the card count — the same goal now occupies three boxes and
 * the edges between them are internal bookkeeping, not structure a reader needs.
 * A group carries the lane that is actually actionable (the first that is not
 * VERIFIED, contract before test before implementation), so clicking it selects
 * the node someone would work on, and keeps every lane's state as pips.
 * Dependencies are re-pointed at the depended-on node's group and self-edges
 * dropped, which is what makes the collapsed graph readable rather than merely
 * smaller. Nodes with no siblings (design, integration, unfaceted) pass through
 * with no lanes.
 */
export function groupFacets(nodes:StateNodeSummary[]):StateGroup[] {
  const byKey = new Map(nodes.map(n => [n.node_key, n]));
  const stem = (key:string) => { const n = byKey.get(key); return n ? stemOf(n) : key; };
  const members = new Map<string, StateNodeSummary[]>();
  for (const node of nodes) {
    const s = stemOf(node);
    if (!members.has(s)) members.set(s, []);
    members.get(s)!.push(node);
  }
  const groups:StateGroup[] = [];
  for (const [key, group] of members) {
    const lane = (facet:StateLane['facet']) => group.find(n =>
      facet === 'content' ? n.node_key === key && n.facet !== 'contract' && n.facet !== 'test' : n.facet === facet);
    const lanes:StateLane[] = LANE_ORDER.map(facet => {
      const node = lane(facet);
      return { facet, node_key: node?.node_key ?? `${key}${facet === 'content' ? '' : '.' + facet}`,
               status: node?.status ?? '', readiness: node?.readiness ?? '',
               revision: node?.revision ?? 0, present: !!node };
    });
    const hasSiblings = group.length > 1;
    // The lane to act on: the first unfinished one, else the implementation.
    const open = lanes.filter(l => l.present && l.status !== 'VERIFIED' && l.status !== 'SUPERSEDED');
    const chosenKey = (open[0] ?? lanes.filter(l => l.present).at(-1))?.node_key ?? key;
    const head = byKey.get(chosenKey) ?? group[0];
    const deps = new Set<string>(), blocked = new Set<string>();
    for (const node of group) {
      for (const d of node.dependencies) if (stem(d) !== key) deps.add(stem(d));
      for (const b of node.blocked_by) if (stem(b) !== key) blocked.add(stem(b));
    }
    const impl = lane('content') ?? head;
    groups.push({ ...head, node_key: key, title: impl.title, domain: key.split('.')[0],
      dependencies: [...deps].sort(), blocked_by: [...blocked].sort(),
      hold: group.find(n => n.hold)?.hold ?? null,
      lanes: hasSiblings ? lanes : [], members: group.map(n => n.node_key).sort() });
  }
  return groups;
}


export function stateLayout(nodes: StateNodeSummary[], domain = '', focus = '', query = '', maximum = 60) {
  const keys = new Set<string>();
  for (const node of nodes) {
    if (keys.has(node.node_key)) throw new Error('Duplicate state node identity');
    keys.add(node.node_key);
  }
  const outgoing = new Map(nodes.map(n => [n.node_key, [] as string[]]));
  for (const node of nodes) {
    for (const dep of node.dependencies) {
      if (!keys.has(dep)) throw new Error('Missing state dependency: ' + dep);
      outgoing.get(dep)!.push(node.node_key);
    }
  }
  const whole = layoutGraph(nodes.map(n => ({ id: n.node_key, transitions: outgoing.get(n.node_key)!.map(to => ({ to })) })), nodes[0]?.node_key ?? '');
  if (whole.edges.some(e => e.back)) throw new Error('State dependency cycle; cannot display it as a DAG');
  const related = focus ? new Set([focus, ...(nodes.find(n => n.node_key === focus)?.dependencies ?? []), ...(outgoing.get(focus) ?? [])]) : null;
  const search = query.trim().toLocaleLowerCase();
  const visible = nodes.filter(n => (!domain || n.domain === domain) && (!related || related.has(n.node_key))
    && (!search || (n.node_key + ' ' + n.title).toLocaleLowerCase().includes(search)));
  if (visible.length > maximum) return { nodes: [], edges: [], width: 0, height: 0, tooLarge: true, count: visible.length, hiddenEdges: 0 };
  const shown = new Set(visible.map(n => n.node_key));
  const layout = layoutGraph(visible.map(n => ({ id: n.node_key,
    transitions: outgoing.get(n.node_key)!.filter(to => shown.has(to)).map(to => ({ to })) })), visible[0]?.node_key ?? '');
  const byKey = new Map(visible.map(n => [n.node_key, n]));
  const boxes = layout.nodes.map(n => ({ ...byKey.get(n.id)!, x: 20 + n.order * (STATE_CARD.width + STATE_CARD.xGap), y: 20 + n.rank * (STATE_CARD.height + STATE_CARD.yGap),
    outsideDependencies: byKey.get(n.id)!.dependencies.filter(k => !shown.has(k)).length }));
  return { nodes: boxes, edges: layout.edges, width: Math.max(304, 40 + layout.widest * (STATE_CARD.width + STATE_CARD.xGap) - STATE_CARD.xGap),
    height: Math.max(196, 40 + layout.rankCount * (STATE_CARD.height + STATE_CARD.yGap) - STATE_CARD.yGap), tooLarge: false, count: visible.length,
    hiddenEdges: whole.edges.filter(e => shown.has(e.from) !== shown.has(e.to)).length };
}


/** External execution is legitimate without a workflow/run, not a broken link. */
export function attemptLabel(attempt:StateAttempt):string {
  return attempt.execution_kind==='external' ? (attempt.harness??'External harness') : (attempt.workflow??'Workflow');
}


export interface ProjectRunSummary {
  project_id:string; observed_at:string; runtime_unavailable:boolean; external_attempts_excluded:number;
  counts:{total:number;running:number;finished:number;failed:number;other:number;unavailable:number};
  execution_counts:{total:number;running:number;finished:number;failed:number;other:number;unavailable:number};
  running_runs:{run_id:string;workflow:string;node_keys:string[];current_node:string|null;started_at:string|null;status:'running'}[];
  running_external:{attempt_id:string;node_key:string;node_revision:number;harness:string;external_id:string;
    reporting_actor:string;status:string;observation_version:number;created_at:string;updated_at:string;
    last_report_at:string|null}[];
  external_counts:{active:number;finished:number;failed:number;other:number;total:number};
  usage:{total_tokens:number|null;prompt_tokens:number|null;completion_tokens:number|null;
    cache_hit_tokens:number|null;cache_miss_tokens:number|null;cache_hit_ratio:number|null;cache_covered_tokens:number;
    usage_turns:number;token_reported_turns:number;cache_reported_turns:number;runs_with_token_usage:number;
    runs_without_token_usage:number;usage_errors:number;partial:boolean};
}
