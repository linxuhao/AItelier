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
  workflow: string; execution_project_id: string; run_id: string | null; status: string;
  artifact_ref: string | null; created_at: string; updated_at: string; title?: string;
}
export interface StateNodeSummary {
  node_key: string; title: string; domain: string; revision: number; contract_hash: string;
  status: string; readiness: string; dependencies: string[]; blocked_by: string[];
  hold: ({ scope: string; reason: string } & Partial<DispatchPolicy>) | null; node_hold: NodeHold;
  criteria_count: number; attempt_count: number; latest_attempt: StateAttempt | null;
  latest_evidence: Record<string, number>; priority: number;
}
export interface StateOverview {
  project: { project_id: string; title: string; source_project_id: string | null };
  source: SourceBinding; policy: DispatchPolicy; nodes: StateNodeSummary[];
  counts: Record<string, number>; readiness_counts: Record<string, number>;
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
export function stateTone(status: string): string {
  // Only the fact is allowed to be green. A successful attempt remains blue.
  return status === 'VERIFIED' ? 'verified' : status === 'CANDIDATE' ? 'candidate'
    : status === 'STALE' ? 'stale' : status === 'SUPERSEDED' ? 'superseded' : 'open';
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
  const boxes = layout.nodes.map(n => ({ ...byKey.get(n.id)!, x: 20 + n.order * 234, y: 20 + n.rank * 142,
    outsideDependencies: byKey.get(n.id)!.dependencies.filter(k => !shown.has(k)).length }));
  return { nodes: boxes, edges: layout.edges, width: Math.max(280, 40 + layout.widest * 234 - 24),
    height: Math.max(150, 40 + layout.rankCount * 142 - 38), tooLarge: false, count: visible.length,
    hiddenEdges: whole.edges.filter(e => shown.has(e.from) !== shown.has(e.to)).length };
}
