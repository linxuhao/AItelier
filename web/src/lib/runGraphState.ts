import { toEpochSeconds } from './format';

/**
 * Fold a run's step instances onto its graph nodes.
 *
 * The flat step timeline this replaces listed every instance in one column, and
 * inside a loop that is unreadable: a fan-out over six tasks shows nine `t_impl`
 * rows, and until skillflow 1.5.41 nothing on the row said which task each one
 * served. Now `loop_item` does, so the loop body can be shown per item.
 *
 * Two honest states this must keep distinct:
 *   - a loop-body node with rows but NO item on any of them -- a run recorded
 *     before the column existed. It is "not recorded", not "one item".
 *   - a node with no rows at all -- never instantiated.
 * Collapsing either into "pending" would make the picture lie about a run.
 */

export interface RunStepRow {
  id: number;
  step_id: string;
  status: string;
  loop_item?: string | null;
  retry_count?: number;
  attempt?: number;
  error?: string;
  claimed_at?: string;
  completed_at?: string;
  completion_seq?: number | null;
}

export type NodeStatus =
  | 'pending' | 'running' | 'completed' | 'failed' | 'mixed' | 'absent';

export interface NodeRunState {
  status: NodeStatus;
  rows: RunStepRow[];
  runs: number; // instances counted (after any item filter)
  retries: number; // summed retry_count
  error: string; // first error worth showing
}

export interface RunGraphState {
  byNode: Record<string, NodeRunState>;
  /** Loop items seen in this run, in the order they were first claimed. */
  items: string[];
  /** True when loop-body rows exist but carry no item (pre-1.5.41 run). */
  unattributed: boolean;
}

/**
 * Milliseconds a step instance took, or null when it never ran / never ended.
 *
 * claimed_at -> completed_at, never created_at: skillflow seeds every step row
 * at RUN creation, so created_at is the run's start for all of them and the
 * difference measures elapsed-since-run-start (which is how several steps of a
 * one-hour run all reported ~45m). Parsing goes through toEpochSeconds because
 * these are SQLite datetimes with no zone marker, which a bare Date.parse reads
 * as local time.
 */
export function rowDuration(row: RunStepRow): number | null {
  const a = toEpochSeconds(row.claimed_at);
  const b = toEpochSeconds(row.completed_at);
  if (a == null || b == null || b < a) return null;
  return Math.round((b - a) * 1000);
}

function fold(rows: RunStepRow[]): NodeRunState {
  if (rows.length === 0) {
    return { status: 'absent', rows, runs: 0, retries: 0, error: '' };
  }
  const has = (s: string) => rows.some((r) => r.status === s);
  let status: NodeStatus;
  if (has('failed')) status = 'failed';
  else if (has('claimed') || has('running')) status = 'running';
  else if (rows.every((r) => r.status === 'completed')) status = 'completed';
  else if (rows.every((r) => r.status === 'pending')) status = 'pending';
  // Some done, some still pending: a loop body mid-fan-out. Saying "completed"
  // here is how a half-finished loop looks finished.
  else status = 'mixed';
  const failed = rows.find((r) => r.status === 'failed' && r.error);
  return {
    status,
    rows,
    runs: rows.length,
    retries: rows.reduce((n, r) => n + (r.retry_count || 0), 0),
    error: failed?.error || rows.find((r) => r.error)?.error || '',
  };
}

/**
 * @param nodeIds       every node in the graph (so nodes with no rows are 'absent')
 * @param loopOf        node id -> loop id, for the nodes inside a loop body
 * @param item          when set, loop-body nodes are folded over this item only
 */
export function runGraphState(
  nodeIds: string[],
  rows: RunStepRow[],
  loopOf: Record<string, string | null | undefined> = {},
  item: string | null = null,
): RunGraphState {
  const items: string[] = [];
  let loopRows = 0;
  let itemRows = 0;
  for (const r of rows) {
    if (!loopOf[r.step_id]) continue;
    loopRows++;
    if (r.loop_item) {
      itemRows++;
      if (!items.includes(r.loop_item)) items.push(r.loop_item);
    }
  }

  const byNode: Record<string, NodeRunState> = {};
  for (const id of nodeIds) {
    let mine = rows.filter((r) => r.step_id === id);
    if (item && loopOf[id]) {
      // Rows with no item are dropped rather than shown under every item --
      // an un-attributed instance belongs to no particular one, and repeating
      // it across items would invent executions that never happened.
      mine = mine.filter((r) => r.loop_item === item);
    }
    byNode[id] = fold(mine);
  }
  return { byNode, items, unattributed: loopRows > 0 && itemRows === 0 };
}
