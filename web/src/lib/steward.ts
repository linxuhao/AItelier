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
  type DriverStatusResponse, type OwnerChannel, type OwnerChannelName, type StewardImpact, type StewardMemoryKind,
  type StewardSchedule,
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
export const SCHEDULE_REFUSALS = ['invalid_cron', 'too_frequent', 'at_in_past', 'invalid_timezone'];

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

// ── Channel-down alert (plugin step 25, design 7.9.2) ──

/** How long a channel is down before the host alerts the owner on another channel. */
export const CHANNEL_ALERT_AFTER_MS = 10 * 60_000;

export type ChannelAlertLine =
  | {key: 'alerted'; minutes: number; at: string; via: OwnerChannelName}
  | {key: 'failed'; minutes: number; error: string}
  | {key: 'noChannel'; minutes: number};

/** The alert line of a channel that is down (null when there is none to show): its down alert
 *  went out (when, through which channel), failed (why), or - down past the alert delay with no
 *  alert - there is no other bound, connected channel to send it through. A channel not bound
 *  or not configured is never alerted about. */
export function channelAlertLine(row: OwnerChannel, rows: OwnerChannel[], now: number): ChannelAlertLine | null {
  const status = row.status;
  if (!status || status.connected || !status.down_since || !row.bound) return null;
  const minutes = Math.max(0, Math.floor((now - Date.parse(status.down_since)) / 60_000));
  if (status.alert_error) return {key: 'failed', minutes, error: status.alert_error};
  if (status.alerted_at && status.alert_via) return {key: 'alerted', minutes, at: status.alerted_at, via: status.alert_via};
  if ((status.detail ?? '').startsWith('not configured') || now - Date.parse(status.down_since) < CHANNEL_ALERT_AFTER_MS) return null;
  const other = rows.some(r => r.channel !== row.channel && r.bound && !r.revoked_at && r.status?.connected);
  return other ? null : {key: 'noChannel', minutes};
}

/** The alert line in words: `tr` is the page's t(). */
export function channelAlertText(line: ChannelAlertLine, tr: (key: string) => string): string {
  if (line.key === 'failed') return tr('steward.chanAlertFailed').replace('{reason}', line.error);
  if (line.key === 'noChannel') return tr('steward.chanAlertNoChannel');
  const time = new Date(line.at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  return tr('steward.chanAlerted').replace('{minutes}', String(line.minutes)).replace('{time}', time)
    .replace('{via}', tr('steward.chan.' + line.via));
}
