/** The owner's steward on the dashboard (plugin step 14, design section 6).
 *
 *  The steward is the driver of the reserved State project `aitelier-home`. That
 *  project is NOT hidden by the backend - it is an ordinary private project - so
 *  keeping it out of the ordinary project lists is layout, decided here and only
 *  here: by the driver's `role`, plus the reserved id itself so the project does
 *  not flash into a list before the driver table has arrived.
 */
import {
  createStewardMemoryRequest, stewardMemoryRequest,
  type DriverStatusResponse, type StewardImpact, type StewardMemoryKind,
} from './api';

export const STEWARD_PROJECT_ID = 'aitelier-home';

type DriverTable = Pick<DriverStatusResponse, 'projects'> | null | undefined;

/** A driver row without a role is a director. */
export function driverRole(table: DriverTable, projectId: string): 'steward' | 'director' {
  return table?.projects[projectId]?.managed?.role === 'steward' ? 'steward' : 'director';
}

export function isStewardProject(projectId: string, table: DriverTable): boolean {
  return projectId === STEWARD_PROJECT_ID || driverRole(table, projectId) === 'steward';
}

/** The steward's project: the one whose driver says so, else the reserved id. */
export function stewardProjectId(table: DriverTable): string {
  return Object.keys(table?.projects ?? {}).find(id => driverRole(table, id) === 'steward') ?? STEWARD_PROJECT_ID;
}

/** Drivers working a turn right now (`running` is the user's "busy"). */
export function busyDriverCount(table: DriverTable): number {
  return Object.values(table?.projects ?? {}).filter(row => row?.managed?.phase === 'running').length;
}

/** The sum of the per-project open-question badges. The per-project numbers are
 *  the server's application of the questions rule (questionRule.ts); this adds
 *  them up and decides nothing, so the total cannot disagree with a badge. */
export function totalOpenQuestions(questions: Record<string, number>): number {
  return Object.values(questions).reduce((sum, n) => sum + (n > 0 ? n : 0), 0);
}

export function impactIsEmpty(impact: Partial<StewardImpact> | null | undefined): boolean {
  return !(impact?.running_subagents?.length || impact?.unsettled_attempts?.length);
}

/** How long a memory request may stay pending before the steward is called offline. */
export const MEMORY_REQUEST_TIMEOUT_MS = 30000;
const MEMORY_REQUEST_POLL_MS = 1000;

/** One owner memory request, run by the steward's host: create it, then poll until
 *  it is done. `null` means it was still pending at the bound - the steward is
 *  offline - and the caller says so; the request stays queued on the server. */
export async function runMemoryRequest(
  kind: StewardMemoryKind, payload: Record<string, unknown> = {},
  options: {timeoutMs?: number; intervalMs?: number} = {},
): Promise<Record<string, any> | null> {
  const timeoutMs = options.timeoutMs ?? MEMORY_REQUEST_TIMEOUT_MS;
  const intervalMs = options.intervalMs ?? MEMORY_REQUEST_POLL_MS;
  const created = await createStewardMemoryRequest(kind, payload);
  if (created.status === 'done') return created.result ?? {};
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, intervalMs));
    const row = await stewardMemoryRequest(created.request_id);
    if (row.status === 'done') return row.result ?? {};
  }
  return null;
}
