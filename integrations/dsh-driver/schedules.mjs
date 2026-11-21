/** The steward's scheduled reminders, its runtime half (step 16, design-steward §7.4).
 *
 *  The backend owns the time: it computes every next_run_at and advances it only inside
 *  fire_steward_schedule, a compare-and-set on the next_run_at the caller saw. This file
 *  only watches the clock: it arms one timer at the earliest due row (and at most a few
 *  minutes ahead, so a change it missed is picked up), fires what is due, and hands the
 *  steward ONE wake for everything that fired - durably, under an id per (row, due time),
 *  so a restart or a replay never wakes it twice for the same run.
 *
 *  Step 25 C11 (design-steward 7.9.4): a reminder whose target is the family group wakes
 *  nobody. Once fired, its text - the owner's exact words, taken from the row by the backend,
 *  never from here - is posted with post_group_notice and sent into the group the way a group
 *  reply is (`sendGroup`, the bridge's one 10-second queue). One that fired more than 30
 *  minutes late is skipped. A skip, a refusal (withheld, group_unbound, group_notice_budget,
 *  group_quota_account, not_fired) or a failed send posts nothing, is never retried (the time
 *  has passed) and gives the steward ONE owner wake to tell its owner. Group notices go out
 *  before the owner wake, each on its own, so one failure never drops another. Two windows
 *  nobody hears of, both a host death (7.9.9 (c)): after the fire commits and before
 *  post_group_notice (the row has moved on; a restart does not fire it again), and after
 *  post_group_notice commits and before sendGroup (the stored notice row shows in the group's
 *  messages though nothing was sent; a replay answers duplicate and sends nothing). */

/** State events about schedules: the runtime's, never a model turn. */
export const SCHEDULE_EVENTS = new Set(['steward_schedule_changed']);
/** Step 25 C11 (7.9.4, owner decision 6 of 7.9.10): a family-group reminder that fires more
 *  than this late is skipped ("该吃药了" three hours late only confuses). */
export const GROUP_NOTICE_LATE_S = 30 * 60;
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
  const fields = Object.fromEntries(['title', 'instruction', 'schedule', 'tz', 'enabled', 'target', 'channel']
    .filter(k => args[k] !== undefined).map(k => [k, args[k]]));
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

const isGroup = fired => fired.schedule?.target === 'family_group';

/** Step 25 C11: post one fired family-group reminder (the host's own request, no header). The
 *  backend takes the text and the group from the row; the key names the run, so a replay is
 *  State's (answered duplicate, sent once). */
export function postGroupNotice(request, fired, signal) {
  const id = fired.schedule.schedule_id;
  return request('/api/state/commands/post_group_notice', { request_key: `group-notice:${id}:${fired.due_at}`,
    schedule_id: id, due_at: fired.due_at }, signal).then(unwrap);
}

/** What the steward is told about a group reminder that did not go out (the owner's audience). */
const NOT_SENT = {
  withheld: '内容里有不能发进群的东西', group_unbound: '记下的那个家庭群已经解绑或换了，这条提醒已停用',
  group_notice_budget: '这个群这一小时的群提醒已满 6 条', group_quota_account: '这个号这一小时发进群的消息已满',
  not_fired: '后端说这一次没有触发', send_failed: '桥没有发出去（例如微信重新登录后、群里还没人说话）',
  state_unreachable: '没连上后端',
};
export function groupNoticeText(fired, answer) {
  const s = fired.schedule;
  const head = `群提醒 ${s.title}（${s.schedule_id}）`;
  if (answer.late_seconds !== undefined) return `${head} 晚了 ${minutes(answer.late_seconds)}，没有发（晚了 30 分钟以上的群提醒不发）。请告诉业主。`;
  const why = NOT_SENT[answer.refused] ?? '没有发出';
  const kind = answer.reason ? `（${answer.reason}）` : '';
  return `${head} 没有发：${answer.refused}${kind}，${why}。请告诉业主。`;
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
  /** `sendGroup(channel, groupRef, text)` (step 25 C11): the plugin's send into a bound group,
   *  true when the bridge took it; without it a group reminder is never sent (and the steward
   *  told). */
  constructor({ request, deliver, sendGroup, log = () => {}, now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout }) {
    Object.assign(this, { request, deliver, sendGroup, log, now, setTimer, clearTimer });
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
    // Group notices first, each isolated: an owner wake that fails never drops one (review F2).
    for (const notice of fired.filter(isGroup)) {
      try { await this.#notice(notice); }
      catch (error) { this.log(`schedule ${notice.schedule.schedule_id}: group reminder failed (${String(error?.message ?? error).slice(0, 200)})`); }
    }
    const owner = fired.filter(f => !isGroup(f));
    if (owner.length) {
      const id = 'schedule:' + owner.map(f => `${f.schedule.schedule_id}:${f.due_at}`).join(',');
      await this.deliver(wakeText(owner), id);
    }
    const next = (fired.length ? await listSchedules(this.request) : rows)
      .filter(r => r.enabled && r.next_run_at).map(r => Date.parse(r.next_run_at)).sort((a, b) => a - b)[0];
    return Math.max(1000, Math.min(RECHECK_MS, next === undefined ? RECHECK_MS : next - this.now()));
  }
  /** One fired family-group reminder: posted and sent, or one owner wake saying why not. */
  async #notice(fired) {
    const run = `group-notice:${fired.schedule.schedule_id}:${fired.due_at}`;
    const tell = answer => this.deliver(groupNoticeText(fired, answer), run + ':not-sent');
    if (Number(fired.late_seconds) > GROUP_NOTICE_LATE_S) {
      this.log(`schedule ${fired.schedule.schedule_id}: group reminder skipped, ${fired.late_seconds}s late`);
      return tell({ late_seconds: Number(fired.late_seconds) });
    }
    let answer;
    try { answer = await postGroupNotice(this.request, fired); }
    catch (error) {
      this.log(`schedule ${fired.schedule.schedule_id}: post_group_notice failed (${String(error?.message ?? error).slice(0, 200)})`);
      return tell({ refused: 'state_unreachable' });
    }
    if (answer?.refused) {
      this.log(`schedule ${fired.schedule.schedule_id}: group reminder refused ${answer.refused}`);
      return tell(answer);
    }
    // A replay of a run already posted: it went to the bridge the first time.
    if (answer?.duplicate || !answer?.posted) return;
    const sent = this.sendGroup ? await Promise.resolve(this.sendGroup(answer.channel, answer.group_ref, answer.text)).catch(() => false) : false;
    if (sent !== true) {
      this.log(`schedule ${fired.schedule.schedule_id}: group reminder not sent by the bridge`);
      return tell({ refused: 'send_failed' });
    }
  }
  close() { this.closed = true; this.clearTimer(this.timer); }
}
