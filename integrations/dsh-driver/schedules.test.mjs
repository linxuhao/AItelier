/** Step 16 (design-steward §7.4): the steward's reminder clock. The backend owns the time
 *  (fire is its compare-and-set); this checks what the runtime does with it: fire only
 *  what is due, one durable wake per run, the timer at the next due row, the tools' shapes. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RECHECK_MS, ScheduleTimer, WAKE_MAX, deleteSchedule, fireDue, setSchedule, wakeText } from './schedules.mjs';

const row = (id, next, extra = {}) => ({ schedule_id: id, version: 1, title: 'T' + id, instruction: '看一眼各项目', schedule: { kind: 'cron', expr: '0 9 * * 1-5' },
  tz: 'Europe/Paris', enabled: true, next_run_at: next, ...extra });

/** A fake backend: rows, and fire as the compare-and-set the contract describes. */
function backend(rows, now) {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path.split('/').at(-1), body]);
    if (path.endsWith('/list_steward_schedules')) return { result: { schedules: rows.map(r => ({ ...r })) } };
    if (path.endsWith('/fire_steward_schedule')) {
      const r = rows.find(x => x.schedule_id === body.schedule_id);
      if (!r || r.next_run_at !== body.expected_next_run_at || Date.parse(r.next_run_at) > now()) return { result: { fired: false, schedule: r } };
      const due = r.next_run_at;
      r.next_run_at = r.schedule.kind === 'once' ? null : new Date(now() + 24 * 3600e3).toISOString();
      if (r.schedule.kind === 'once') r.enabled = false;
      r.version++;
      return { result: { fired: true, schedule: { ...r }, due_at: due, late_seconds: (now() - Date.parse(due)) / 1000, missed: r.missedNext ?? 0 } };
    }
    return { result: { ok: true, ...body } };
  };
  return { request, calls };
}

test('fireDue: only enabled rows whose time has come, each under a key naming its run; a refused CAS is not a fire', async () => {
  const t = Date.parse('2026-11-18T09:00:30Z');
  const rows = [row('a', '2026-11-18T09:00:00Z'), row('b', '2026-11-18T10:00:00Z'), row('c', '2026-11-18T08:00:00Z', { enabled: false }),
    row('d', null)];
  const { request, calls } = backend(rows, () => t);
  const fired = await fireDue(request, rows.map(r => ({ ...r })), t);
  assert.deepEqual(fired.map(f => f.schedule.schedule_id), ['a']);
  assert.deepEqual(calls, [['fire_steward_schedule', { request_key: 'fire:a:2026-11-18T09:00:00Z', schedule_id: 'a', expected_next_run_at: '2026-11-18T09:00:00Z' }]]);
  // The owner moved it between our read and our fire: the backend says no, nothing is delivered.
  const stale = [row('a', '2026-11-18T09:00:00Z')];
  assert.deepEqual(await fireDue(backend([row('a', '2026-11-18T09:30:00Z')], () => t).request, stale, t), []);
});

test('the clock: one durable wake for everything that fired, delivered once per run; the timer at the next due row', async () => {
  let clock = Date.parse('2026-11-18T09:00:05Z');
  const rows = [row('a', '2026-11-18T09:00:00Z'), row('b', '2026-11-18T09:00:00Z', { schedule: { kind: 'once', at: '2026-11-18T09:00:00Z' } }),
    row('c', '2026-11-18T09:02:00Z')];
  const { request } = backend(rows, () => clock);
  const delivered = [], timers = [];
  const timer = new ScheduleTimer({ request, now: () => clock, deliver: async (text, id) => delivered.push([id, text]),
    setTimer: (fn, ms) => (timers.push(ms), { fn, ms }), clearTimer: () => {} });
  await timer.refresh();
  assert.equal(delivered.length, 1, 'two rows due at once: ONE wake');
  assert.equal(delivered[0][0], 'schedule:a:2026-11-18T09:00:00Z,b:2026-11-18T09:00:00Z');
  assert.match(delivered[0][1], /定时提醒 Ta（a），本应 .*触发：看一眼各项目/);
  assert.equal((delivered[0][1].match(/定时提醒 T/g) ?? []).length, 2, 'one paragraph each');
  assert.equal(timers.at(-1), 2 * 60 * 1000 - 5000, 'armed at the next due row (c at 09:02)');
  // Nothing new is due: a second look delivers nothing (b was a once and is off; a moved a day on).
  await timer.refresh();
  assert.equal(delivered.length, 1);
  // Far in the future: the timer still looks again within RECHECK_MS.
  clock = Date.parse('2026-11-18T09:03:00Z');
  await timer.refresh();
  assert.equal(delivered.length, 2, 'c fired');
  assert.equal(timers.at(-1), RECHECK_MS);
  timer.close();
  await timer.refresh();
  assert.equal(delivered.length, 2, 'a closed clock does nothing');
});

test('the wake says how late and how many runs were missed while the steward was down', () => {
  const text = wakeText([{ schedule: row('a', null, { title: '周报' }), due_at: '2026-11-17T08:00:00Z', late_seconds: 26 * 3600, missed: 3 }]);
  assert.match(text, /定时提醒 周报（a），本应 .* 触发，晚了 26 小时，期间错过 3 次：看一眼各项目/);
  assert.doesNotMatch(wakeText([{ schedule: row('a', null), due_at: '2026-11-17T08:00:00Z', late_seconds: 20, missed: 0 }]), /晚了|错过/,
    'on time: no lateness line');
});

test('the tools: create, change with the version seen, delete; a change without a version writes nothing', async () => {
  const { request, calls } = backend([], () => 0);
  await setSchedule(request, { title: '周报', instruction: '总结各项目', schedule: { kind: 'cron', expr: '0 9 * * 1' } });
  await setSchedule(request, { schedule_id: 'sch-1', expected_version: 2, enabled: false });
  assert.deepEqual(await setSchedule(request, { schedule_id: 'sch-1', enabled: true }), { refused: 'expected_version_required', reason: 'read it with schedule_list first' });
  await deleteSchedule(request, { schedule_id: 'sch-1', expected_version: 3 });
  assert.deepEqual(calls.map(([action, body]) => [action, { ...body, request_key: undefined }]), [
    ['create_steward_schedule', { request_key: undefined, title: '周报', instruction: '总结各项目', schedule: { kind: 'cron', expr: '0 9 * * 1' } }],
    ['update_steward_schedule', { request_key: undefined, schedule_id: 'sch-1', expected_version: 2, enabled: false }],
    ['delete_steward_schedule', { request_key: undefined, schedule_id: 'sch-1', expected_version: 3 }]]);
  assert.ok(new Set(calls.map(([, body]) => body.request_key)).size === 3, 'each change its own request key');
});

test('review 337: one wake holds at most WAKE_MAX reminders, oldest due first; the rest come on the next look', async () => {
  let clock = Date.parse('2026-11-18T09:30:00Z');
  const rows = Array.from({ length: WAKE_MAX + 3 }, (_, i) => row('r' + String(i).padStart(2, '0'), `2026-11-18T09:${String(i).padStart(2, '0')}:00Z`));
  const { request } = backend(rows, () => clock);
  const delivered = [], timers = [];
  const timer = new ScheduleTimer({ request, now: () => clock, deliver: async (text, id) => delivered.push([id, text]),
    setTimer: (fn, ms) => (timers.push(ms), {}), clearTimer: () => {} });
  await timer.refresh();
  assert.equal((delivered[0][1].match(/定时提醒 T/g) ?? []).length, WAKE_MAX);
  assert.match(delivered[0][0], /^schedule:r00:/, 'oldest due first');
  assert.equal(timers.at(-1), 1000, 'more are due: look again right away');
  await timer.refresh();
  assert.equal((delivered[1][1].match(/定时提醒 T/g) ?? []).length, 3);
  assert.match(wakeText([{ schedule: row('x', null, { updated_by: 'steward' }), due_at: '2026-11-18T09:00:00Z', late_seconds: 0, missed: 0 }]),
    /（最后由你修改）/);
  timer.close();
});
