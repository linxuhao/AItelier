/** Resuming workers a restart interrupted, in their own sessions (plugin step 6).
 *
 *  A host restart ends every native child: they run inside the host process. Their
 *  plans stay bound to sessions nobody runs, and the director could only take the plan
 *  over itself or dispatch a fresh worker - both throw the worker's context away. DSH
 *  cold-resumes a stopped continuable direct child when its parent messages it, so at
 *  boot the director messages each interrupted worker once, with a recovery note.
 *
 *  Only on a positive signal that the worker died with the old process, all of:
 *  - no live agent in this process holds the session;
 *  - its log has no event since this process started (nothing here touched it);
 *  - its last turn never completed (still open, or ended without `completed`): it was
 *    working when the process ended, not parked by its own choice. A worker that ended
 *    its turn with work left already handed its plan back; that path is unchanged.
 *  - it is a continuable direct child of the director (the director's own catalog).
 *  DSH's catalog carries no running state; the child's own log is where "running"
 *  is written, so that is what is read.
 *
 *  Plans whose work item settled, or whose candidate report exists, are left alone. A
 *  worker is resumed at most `maxResumes` times over all restarts (resume/<id>.json). */
import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

/** What the old process left in a child's log: when it last wrote, and whether the
 *  last turn was cut off. `events` are the raw session events ({type, time, data}). */
export function lastTurn(events = []) {
  let lastEventAt = 0, open = false, completed;
  for (const event of events) {
    if (Number.isFinite(event?.time)) lastEventAt = Math.max(lastEventAt, event.time);
    if (event?.type === 'turn/start') { open = true; completed = undefined; }
    if (event?.type === 'turn/end') { open = false; completed = event.data?.reason?.kind === 'completed'; }
  }
  return { lastEventAt, interrupted: open || completed === false, everTurned: open || completed !== undefined };
}

/** The plans whose bound worker may be resumed, before any session is read: unfinished,
 *  not settled, no candidate report, bound to a child that is not the director and not
 *  live here. */
export function resumeCandidates({ plans, settled = new Set(), director, isLive }) {
  return plans.filter(plan => plan.child_session && plan.child_session !== director
    && plan.remaining?.length && !settled.has(plan.attempt_id) && !plan.finish_reported && !isLive(plan.child_session));
}

/** Why a candidate is not resumed (undefined: resume it). */
export function refusal({ row, turn, startedAt, resumes, maxResumes }) {
  if (!row) return 'not_a_direct_child';
  if (row.mode !== 'continuable') return 'not_continuable';
  if (!turn) return 'log_unreadable';
  if (turn.lastEventAt >= startedAt) return 'active_since_this_process_started';
  if (!Number.isFinite(resumes)) return 'resume_record_unreadable';
  if (!turn.interrupted) return 'not_interrupted';
  if (resumes >= maxResumes) return 'resume_limit_reached';
  return undefined;
}

/** The recovery note a resumed worker receives. */
export function recoveryNote(plan) {
  return [
    `You were interrupted: the host process that ran you restarted. This message resumes you in your own session, with your context intact.`,
    `Your plan is attempt_id ${plan.attempt_id}${plan.node_key ? ` (node ${plan.node_key})` : ''}.`,
    `Before anything else: your worktree and any process you started may be gone or changed. A background job you started (a container, a test run, a service) may have died with the host or may still be running without you. Call todo_status for ${plan.attempt_id}, then re-read git status and git log in your worktree and re-measure anything you were relying on before you continue.`,
    `If your worktree is gone, report the plan failed with todo_report and stop: do not recreate the worktree.`,
    `Otherwise continue from your current item. Do not repeat items that already passed.`,
  ].join('\n');
}

/** How often each worker was resumed, persisted (resume/<child>.json). */
export class ResumeRecords {
  constructor(dir) { this.dir = join(dir, 'resume'); }
  #file(child) { return join(this.dir, encodeURIComponent(child) + '.json'); }
  async count(child) {
    try { return JSON.parse(await readFile(this.#file(child), 'utf8')).resumes?.length ?? 0; }
    catch (error) { if (error.code === 'ENOENT') return 0; return Infinity; /* unreadable: never resume blind */ }
  }
  async add(child, entry) {
    await mkdir(this.dir, { recursive: true });
    let record = { child, resumes: [] };
    try { record = JSON.parse(await readFile(this.#file(child), 'utf8')); } catch {}
    record.resumes.push(entry);
    await writeFile(this.#file(child) + '.tmp', JSON.stringify(record, null, 2), { mode: 0o600 });
    await rename(this.#file(child) + '.tmp', this.#file(child));
  }
}
