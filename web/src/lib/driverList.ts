/** The driver ordering rule the driver table has always applied, shared with
 *  the State DAG list so there is ONE rank table and not a second opinion.
 *
 *  Primary key: the managed driver's lifecycle phase, running first.
 *  Tie-break: the most recently updated driver row (State's own `updated_at`),
 *  then the project's catalog `created_at`, then the project id.
 *
 *  The phase labels are the user's three states (running/idle/stopped) plus the
 *  transitional ones the runtime really reports. A project with no driver row
 *  at all ranks below `stopped`: it was never started, so it is the least
 *  active thing in the list.
 */
export const DRIVER_PHASE_RANK: Record<string, number> = {
  running: 0, waiting: 1, starting: 2, stopping: 3, failed: 4, stopped: 5,
};
export const NO_DRIVER_RANK = 6;

export function driverPhaseRank(phase: string | null | undefined): number {
  return (phase && DRIVER_PHASE_RANK[phase]) ?? NO_DRIVER_RANK;
}

export type DriverState = 'running' | 'idle' | 'starting' | 'stopping' | 'failed' | 'stopped';

/** `waiting` is a live driver with nothing to do: the user's "idle". */
export function driverState(phase: string | null | undefined): DriverState {
  switch (phase) {
    case 'running': return 'running';
    case 'waiting': return 'idle';
    case 'starting': return 'starting';
    case 'stopping': return 'stopping';
    case 'failed': return 'failed';
    default: return 'stopped';
  }
}

const LABEL_KEYS: Record<DriverState, string> = {
  running: 'driverStateRunning', idle: 'driverStateIdle', starting: 'driverStateStarting',
  stopping: 'driverStateStopping', failed: 'driverStateFailed', stopped: 'driverStateStopped',
};

export function driverStateLabelKey(phase: string | null | undefined): string {
  return LABEL_KEYS[driverState(phase)];
}

export interface DriverTableRow {
  managed?: {phase?: string; updated_at?: string} | null;
}
export interface DriverTableLike {
  projects: Record<string, DriverTableRow | undefined>;
}
export interface DagRowLike {
  project_id: string;
  /** The catalog carries State's `state_projects.created_at`. */
  created_at?: string;
}

function stamp(iso: string | undefined): number {
  if (!iso) return 0;
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Running first, then most recently updated, then project id. */
export function sortDagRows<T extends DagRowLike>(rows: readonly T[], table: DriverTableLike | null): T[] {
  const when = new Map<string, number>();
  for (const row of rows) {
    const driver = table?.projects[row.project_id]?.managed;
    when.set(row.project_id, stamp(driver?.updated_at) || stamp(row.created_at));
  }
  return [...rows].sort((a, b) =>
    driverPhaseRank(table?.projects[a.project_id]?.managed?.phase) -
    driverPhaseRank(table?.projects[b.project_id]?.managed?.phase) ||
    (when.get(b.project_id) ?? 0) - (when.get(a.project_id) ?? 0) ||
    a.project_id.localeCompare(b.project_id));
}
