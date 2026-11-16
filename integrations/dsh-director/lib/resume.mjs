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
 *  - its last turn was cut off by the process ending: still open, or closed by DSH
 *    as `interrupted`. A turn that ENDED in the old process (`completed`, `error`,
 *    anything else) is not resumed: that end already handed the plan back to the
 *    director, who decides; that path is unchanged.
 *    Measured on the pinned DSH (2026-11-16): a worker that stops itself with work
 *    left writes turn/end `completed`; a turn cut by a model error writes turn/end
 *    `error`; a host SIGTERM'd mid-turn writes NO turn/end (its log on disk ends in
 *    step/start), and DSH's own read of that log (sessionQuery.readSession) closes
 *    the open turn as turn/end `interrupted`, stamped with the last event's time. So
 *    `interrupted` (or a still-open turn) is exactly the restart case; any other kind
 *    is the worker's or the model's own end, and is never resumed.
 *  - it is a continuable direct child of the director (the director's own catalog).
 *  DSH's catalog carries no running state; the child's own log is where "running"
 *  is written, so that is what is read.
 *
 *  Plans whose work item settled, or whose candidate report exists, are left alone. A
 *  worker gets at most `maxResumes` resume ATTEMPTS over all restarts (resume/<id>.json).
 *  An attempt is recorded before the message is sent, so a crash right after a
 *  delivery can never allow one more; the price, stated: a send that fails also uses
 *  an attempt. The record says which attempts were delivered, so the reason given when
 *  the attempts are used up is honest (resume_limit_reached: every attempt delivered;
 *  resume_attempts_exhausted: some or all never reached the worker). */
import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

/** What the old process left in a child's log: when it last wrote, and whether its
 *  last turn was cut off by that process ending (open, or closed `interrupted`).
 *  `events` are the session events as DSH reads them ({type, time, data}). */
export function lastTurn(events = []) {
  let lastEventAt = 0, cut = false;
  for (const event of events) {
    if (Number.isFinite(event?.time)) lastEventAt = Math.max(lastEventAt, event.time);
    if (event?.type === 'turn/start') cut = true;
    if (event?.type === 'turn/end') cut = event.data?.reason?.kind === 'interrupted';
  }
  return { lastEventAt, interrupted: cut };
}

/** The plans whose bound worker may be resumed, before any session is read: unfinished,
 *  not settled, no candidate report, bound to a child that is not the director and not
 *  live here. */
export function resumeCandidates({ plans, settled = new Set(), director, isLive }) {
  return plans.filter(plan => plan.child_session && plan.child_session !== director
    && plan.remaining?.length && !settled.has(plan.attempt_id) && !plan.finish_reported && !isLive(plan.child_session));
}

/** Why a candidate is not resumed (undefined: resume it). */
export function refusal({ row, turn, startedAt, record, maxResumes }) {
  if (!row) return 'not_a_direct_child';
  if (row.mode !== 'continuable') return 'not_continuable';
  if (!turn) return 'log_unreadable';
  if (turn.lastEventAt >= startedAt) return 'active_since_this_process_started';
  if (!record) return 'resume_record_unreadable';
  if (!turn.interrupted) return 'not_interrupted';
  if (record.attempts >= maxResumes) return record.delivered === record.attempts ? 'resume_limit_reached' : 'resume_attempts_exhausted';
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
  async #read(child) {
    try { return JSON.parse(await readFile(this.#file(child), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return { child, resumes: [] }; return undefined; }
  }
  async #write(child, record) {
    await mkdir(this.dir, { recursive: true });
    await writeFile(this.#file(child) + '.tmp', JSON.stringify(record, null, 2), { mode: 0o600 });
    await rename(this.#file(child) + '.tmp', this.#file(child));
  }
  /** {attempts, delivered}, or undefined when the record is unreadable (never resume blind). */
  async stats(child) {
    const record = await this.#read(child);
    if (!Array.isArray(record?.resumes)) return undefined;
    return { attempts: record.resumes.length, delivered: record.resumes.filter(entry => entry.delivered).length };
  }
  /** Record one attempt, before its message is sent. */
  async add(child, entry) {
    const record = (await this.#read(child)) ?? { child, resumes: [] };
    record.resumes.push({ ...entry, delivered: false });
    await this.#write(child, record);
  }
  /** Mark the latest attempt delivered, once its message was accepted. */
  async delivered(child) {
    const record = await this.#read(child);
    if (!record?.resumes?.length) return;
    record.resumes[record.resumes.length - 1].delivered = true;
    await this.#write(child, record);
  }
}
