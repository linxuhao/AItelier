/** The owner's steward on the dashboard (plugin step 14, design section 6).
 *
 *  The steward is the driver of the reserved State project `aitelier-home`. That
 *  project is NOT hidden by the backend - it is an ordinary private project - so
 *  keeping it out of the ordinary project lists is layout, decided here and only
 *  here: by the driver's `role`, plus the reserved id itself so the project does
 *  not flash into a list before the driver table has arrived.
 */
import {
  ApiError, createStewardMemoryRequest, stewardMemoryRequest,
  type DriverStatusResponse, type StewardImpact, type StewardMemoryKind, type StewardSchedule,
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

// ── Scheduled reminders (plugin step 16) ──

/** One-click cron expressions; the owner can still write any 5-field expression. A preset
 *  with `fill` also fills the title and instruction when they are empty, from the texts
 *  steward.schedPresetTitle.<id> / steward.schedPresetInstruction.<id> (design 7.9.3: the daily
 *  brief - what goes into it is steward.md's rule, not the reminder's). */
export const SCHEDULE_PRESETS: readonly {id: string; expr: string; fill?: boolean}[] = [
  {id: 'daily', expr: '0 9 * * *'},
  {id: 'weekdays', expr: '0 9 * * 1-5'},
  {id: 'monday', expr: '0 9 * * 1'},
  {id: 'monthly', expr: '0 9 1 * *'},
  {id: 'brief', expr: '0 8 * * *', fill: true},
];

/** The refusal codes the schedule endpoints give with a 422 (each has a steward.schedReason.* text). */
export const SCHEDULE_REFUSALS = ['invalid_cron', 'too_frequent', 'at_in_past', 'invalid_timezone',
  // Step 25 C11: a reminder to the family group.
  'no_group_bound', 'withheld', 'notice_too_long', 'too_many_group_reminders', 'group_unbound', 'channel_required',
  // Step 23.1 (design 7.7.24): several groups - the reminder names one.
  'group_required', 'unknown_group'];
/** The refusal codes the group routes give (each has a steward.groupReason.* text; step 23.1). */
export const GROUP_REFUSALS = ['too_many_groups', 'group_label_taken', 'group_already_bound', 'invalid_label'];

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

/** A run time for people, on the row's own zone's wall clock; '—' when there is none. */
export function formatRunAt(iso: string | null | undefined, tz?: string): string {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  try { return at.toLocaleString(undefined, {timeZone: tz, dateStyle: 'medium', timeStyle: 'short'}); }
  catch { return at.toLocaleString(); }
}

/** ISO instant -> the value of an <input type="datetime-local"> in the browser's zone. */
export function toLocalInput(iso: string | null | undefined): string {
  const at = new Date(iso ?? '');
  if (!iso || Number.isNaN(at.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** The datetime-local value (browser zone) -> ISO UTC; '' when it is empty or not a time. */
export function fromLocalInput(value: string): string {
  const at = new Date(value);
  return value && !Number.isNaN(at.getTime()) ? at.toISOString() : '';
}

/** The current row a version-conflict refusal carries, or null when this is something else. */
export function scheduleConflict(error: unknown): StewardSchedule | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail as {schedule?: StewardSchedule} | null;
  return detail?.schedule && typeof detail.schedule === 'object' ? detail.schedule : null;
}

/** A 422's {code, reason}, or null. */
export function scheduleRefusal(error: unknown): {code: string; reason: string} | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  const detail = error.detail as {code?: unknown; reason?: unknown} | null;
  return detail && typeof detail === 'object' && typeof detail.code === 'string'
    ? {code: detail.code, reason: typeof detail.reason === 'string' ? detail.reason : ''} : null;
}
