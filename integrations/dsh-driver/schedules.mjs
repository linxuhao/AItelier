/** The steward's scheduled reminders, its runtime half (step 16, design-steward §7.4).
 *
 *  The backend owns the time: it computes every next_run_at and advances it only inside
 *  fire_steward_schedule, a compare-and-set on the next_run_at the caller saw. This file
 *  only watches the clock: it arms one timer at the earliest due row (and at most a few
 *  minutes ahead, so a change it missed is picked up), fires what is due, and hands the
 *  steward ONE wake for everything that fired - durably, under an id per (row, due time),
 *  so a restart or a replay never wakes it twice for the same run. */

/** State events about schedules: the runtime's, never a model turn. */
export const SCHEDULE_EVENTS = new Set(['steward_schedule_changed']);
/** The longest the timer sleeps before it looks again (the pump's own cadence). */
export const RECHECK_MS = 4 * 60 * 1000;
/** At most this many reminders in one wake; the rest fire on the next look, oldest due first
 *  (design 7.4: a wake stays bounded). */
export const WAKE_MAX = 10;

const unwrap = r => r?.result ?? r;
const refused = error => ({ refused: 'state_refused', reason: String(error?.message ?? error).slice(0, 500) });

export async function listSchedules(request, signal) {
  return (unwrap(await request('/api/state/query/list_steward_schedules', { include_disabled: true }, signal)).schedules ?? []);
}

let keys = 0;
const key = what => `${what}:${Date.now().toString(36)}:${(keys++).toString(36)}`;

/** Create (no schedule_id) or change one reminder. A change needs the version the steward saw. */
export function setSchedule(request, args, signal) {
  const fields = Object.fromEntries(['title', 'instruction', 'schedule', 'tz', 'enabled'].filter(k => args[k] !== undefined).map(k => [k, args[k]]));
  if (!args.schedule_id) return request('/api/state/commands/create_steward_schedule', { request_key: key('schedule-create'), ...fields }, signal).then(unwrap, refused);
  if (!Number.isInteger(args.expected_version)) return Promise.resolve({ refused: 'expected_version_required', reason: 'read it with schedule_list first' });
  return request('/api/state/commands/update_steward_schedule', { request_key: key('schedule-update'), schedule_id: args.schedule_id,
    expected_version: args.expected_version, ...fields }, signal).then(unwrap, refused);
}

export function deleteSchedule(request, { schedule_id, expected_version }, signal) {
  return request('/api/state/commands/delete_steward_schedule', { request_key: key('schedule-delete'), schedule_id, expected_version }, signal)
    .then(unwrap, refused);
}

/** Fire every enabled row whose next_run_at has come; return the ones the backend says fired.
 *  The request key names the run, so a retried fire of the same run is State's replay. */
export async function fireDue(request, rows, now = Date.now(), signal, max = WAKE_MAX, log = () => {}) {
  const fired = [];
  const due = rows.filter(r => r.enabled && r.next_run_at && Date.parse(r.next_run_at) <= now)
    .sort((a, b) => Date.parse(a.next_run_at) - Date.parse(b.next_run_at) || (a.schedule_id < b.schedule_id ? -1 : 1));
  for (const row of due) {
    if (fired.length >= max) break;
    // One row's failure (a 4xx, the network) never skips the others: it is logged and that
    // row is looked at again on the next look (review 340 (b)).
    let answer;
    try {
      answer = unwrap(await request('/api/state/commands/fire_steward_schedule', { request_key: `fire:${row.schedule_id}:${row.next_run_at}`,
        schedule_id: row.schedule_id, expected_next_run_at: row.next_run_at }, signal));
    } catch (error) {
      log(`schedule ${row.schedule_id}: fire failed (${String(error?.message ?? error).slice(0, 200)})`);
      continue;
    }
    if (answer?.fired) fired.push({ ...answer, schedule: answer.schedule ?? row, due_at: answer.due_at ?? row.next_run_at });
  }
  return fired;
}

const local = (iso, tz) => {
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: tz, dateStyle: 'short', timeStyle: 'short' }).format(new Date(iso)) + ` (${tz})`; }
  catch { return iso; }
};
const minutes = seconds => seconds >= 3600 ? `${Math.round(seconds / 360) / 10} 小时` : `${Math.max(1, Math.round(seconds / 60))} 分钟`;

/** One wake for everything that fired, one paragraph each (design §7.4). */
export function wakeText(fired) {
  return '定时提醒到了：\n\n' + fired.map(f => {
    const s = f.schedule, late = Number(f.late_seconds) > 90 ? `，晚了 ${minutes(Number(f.late_seconds))}` : '';
    const missed = Number(f.missed) > 0 ? `，期间错过 ${f.missed} 次` : '';
    // Who last changed it, so a reminder its owner did not write explains itself (review 337).
    const by = s.updated_by === 'steward' ? '（最后由你修改）' : '';
    return `定时提醒 ${s.title}（${s.schedule_id}）${by}，本应 ${local(f.due_at, s.tz)} 触发${late}${missed}：${s.instruction}`;
  }).join('\n\n');
}

/** Watches the clock for the steward. `deliver(text, id)` hands a wake over durably. */
export class ScheduleTimer {
  constructor({ request, deliver, log = () => {}, now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout }) {
    Object.assign(this, { request, deliver, log, now, setTimer, clearTimer });
    this.timer = undefined; this.closed = false; this.running = Promise.resolve();
  }
  /** Look again: fire what is due, then arm the timer at the next due row (or RECHECK_MS). */
  refresh() {
    if (this.closed) return this.running;
    this.running = this.running.then(() => this.#tick()).catch(error => this.log('schedule: ' + String(error?.message ?? error).slice(0, 300)));
    return this.running;
  }
  async #tick() {
    if (this.closed) return;
    // Whatever happens below, the clock is armed again (review 340 (b)).
    let wait = RECHECK_MS;
    try { wait = await this.#look(); }
    finally {
      this.clearTimer(this.timer);
      if (!this.closed) { this.timer = this.setTimer(() => void this.refresh(), wait); this.timer?.unref?.(); }
    }
    return { wait };
  }
  async #look() {
    const rows = await listSchedules(this.request);
    const fired = await fireDue(this.request, rows, this.now(), undefined, WAKE_MAX, this.log);
    if (fired.length) {
      const id = 'schedule:' + fired.map(f => `${f.schedule.schedule_id}:${f.due_at}`).join(',');
      await this.deliver(wakeText(fired), id);
    }
    const next = (fired.length ? await listSchedules(this.request) : rows)
      .filter(r => r.enabled && r.next_run_at).map(r => Date.parse(r.next_run_at)).sort((a, b) => a - b)[0];
    return Math.max(1000, Math.min(RECHECK_MS, next === undefined ? RECHECK_MS : next - this.now()));
  }
  close() { this.closed = true; this.clearTimer(this.timer); }
}
