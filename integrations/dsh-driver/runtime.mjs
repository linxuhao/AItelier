/** AItelier's State adapter for dsh-director: the `directorHost` this deployment's
 *  drivers run with.
 *
 *  The director itself (role table, tool tiers, todo plans and batches, continuation,
 *  idle review and dormancy, process watches, child handback) is the generic plugin
 *  in ../dsh-director (plugin design step 2). This file keeps what is AItelier's: the
 *  State subscription and snapshot, the project and driver inboxes, the user message
 *  tool, the completion bridge and model routes, project_work, the driver goal, and
 *  AItelier's wording (aitelier-texts.mjs). The production profile inserts both
 *  plugins: dsh-director, then this adapter, which provides `directorHost`.
 *
 *  Every name this file exported before the split is still exported here (the ones
 *  that moved are re-exported with this deployment's labels bound) except one: `boot`.
 *  Booting is dsh-director's now - its index.mjs exports boot(ctx, { host }), a
 *  different signature - and nothing imported this file's boot. The added names are
 *  SESSION_ENV, aitelierHost and attemptWorkState. exports.test.mjs pins both lists. */
import { installHostModel, modelRetryBudgetMs, rootReasoningEffort } from './model.mjs';
import { rolesFile } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { appendGoalSeedRecord, bindDriverGoal, driverGoalSection, goalMdCommit } from './goal.mjs';
import { labels, texts, stateInjectionSource, runtimeEventSource, todoBriefSource, continueSource,
  askTheUser, continuationQuestion, reviewInstruction } from './aitelier-texts.mjs';
import * as core from '../dsh-director/lib/core.mjs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'aitelier-state-driver';
// The adapter only provides a service; the director plugin injects DSH's.
export const inject = [];

/** AItelier's halves of the role prompts (plugin step 4): the State protocol after the
 *  generic director.md, and the worker's State limits after the generic worker.md. */
const stateSkill = await readFile(new URL('./state.md', import.meta.url), 'utf8');
const workerStateSkill = await readFile(new URL('./worker-state.md', import.meta.url), 'utf8');
const goalFile = fileURLToPath(new URL('./goal.md', import.meta.url));
const goalTemplate = await readFile(goalFile, 'utf8');
export const driverGoal = project => goalTemplate.replaceAll('{{project_id}}', project);
export { stateInjectionSource, runtimeEventSource, todoBriefSource, continueSource, askTheUser, continuationQuestion, reviewInstruction };

/** Does an event tick carry anything worth waking the driver for?
 *
 *  A tick with no events, no driver inbox items and no project inbox is a
 *  heartbeat: steering it still creates a user message, so the harness reports
 *  pending input and the driver spends a turn on nothing. A driver woken with an
 *  empty payload either spins or learns to distrust notifications.
 *  @param result - one `/wait` payload.
 *  @returns true when there is something to read.
 */
export function eventCarriesSomething(result) {
  return Boolean(result?.events?.length || result?.driver_inbox?.length || result?.project_inbox?.items?.length);
}

/** Is a tick only this driver's own State writes coming back?
 *
 *  Every claim, evidence record, note or attempt report a director makes is
 *  published as a State event and arrives on its own subscription. Woken by it,
 *  the director reads what it just did and ends the turn - and that turn then
 *  draws a continuation question (measured 2026-11-05: about half of all wakes).
 *  Own means exactly "the event says I wrote it": `actor` is `driver:<me>`.
 *  An event with no `actor` is a system event, whatever else its payload names -
 *  `lease_expired` carries the claim holder's `driver_id` and no actor, and it
 *  must reach the holder too, so a `driver_id` alone is never own. Any inbox item
 *  makes the tick input.
 *  @param result - one `/wait` payload (with `project_inbox` when fetched).
 *  @param driverId - this driver's id.
 *  @returns true when the tick should only advance the cursor. */
export function ownEchoOnly(result, driverId) {
  if (!driverId || !result?.events?.length || result.driver_inbox?.length || result.project_inbox?.items?.length) return false;
  const me = 'driver:' + driverId;
  return result.events.every(({ payload = {} }) => payload.actor === me);
}

/** The director's project inbox, bounded and self-describing.
 *
 *  The raw `list_director_messages` page is everything about every message: full
 *  bodies plus each delivery's quorum bookkeeping (`acks`, `pending_drivers`,
 *  `legacy_ack`, `quorum_met`, the same ids repeated three times). Measured
 *  2026-11-05: 20 items, `matched_total` 91, 42,304 characters of `project_inbox`
 *  (42,358 for the whole injected message), re-sent on EVERY
 *  `director_message_received` wake - the largest consumer of the resident
 *  director's context and one that grows with project history. Only 24,894 of those
 *  characters are bodies; the rest is per-item duplication and quorum bookkeeping.
 *
 *  What a director needs from a wake is only: which messages, what they are about
 *  (identity), enough body to decide whether to read the whole thing (an excerpt),
 *  and how large a page this is (`items` vs `matched_total`, `has_more`). So the
 *  projection keeps identity, clips the body, and states the page. Nothing is
 *  dropped silently: an item that carries no body still carries its identity and is
 *  still counted, `bodies_withheld` says why, and `items_omitted` counts anything
 *  the ceiling kept out.
 */
export const INBOX_EXCERPT_CHARS = 240;
export const INBOX_SUPERSEDED_EXCERPT_CHARS = 96;
export const INBOX_SUBJECT_CHARS = 160;
export const INBOX_CEILING_CHARS = 12000;
export const INBOX_PAGE_ITEMS = 20;
export const INBOX_SCAN_PAGES = 5;

const inboxText = value => (typeof value === 'string' ? value : value == null ? '' : String(value));
const clip = (value, max) => { const text = inboxText(value); return text.length > max ? text.slice(0, max) : text; };
const sequence = item => { const seq = Number(item?.delivery?.delivery_seq); return Number.isFinite(seq) ? seq : -1; };

/** Every matched delivery of the project inbox, the newest `keep` kept for injection.
 *
 *  `list_director_messages` pages by `delivery_seq` ASCENDING and offers no
 *  newest-first or body-less option (core/director_messaging.py takes project_id,
 *  after, limit, delivery_mode, statuses, needs_my_ack, ack_mode - nothing that
 *  orders by recency). So `limit: 20` on a 91-match inbox returns the OLDEST 20, and
 *  a message that arrives today sits on page five and would never be injected -
 *  exactly the delivery a director most needs. The cursor is the only way to the
 *  end of the matches, so walk it: `limit: 100` per request (the server's maximum),
 *  at most `pages` requests, then keep the newest `keep`.
 *
 *  Transport grows (a 91-match inbox is one 100-item response per wake); context
 *  does not - the projection still carries one page, and it states which page.
 */
export async function listProjectInbox(request, project, {
  limit = 100, pages = INBOX_SCAN_PAGES, keep = INBOX_PAGE_ITEMS, statuses = ['unread', 'acknowledged'],
} = {}) {
  const items = [];
  let after = 0, matched_total = 0, next_after = 0, more = false, started = false;
  for (let page = 0; page < pages; page++) {
    const response = await request('/api/state/query/list_director_messages', {
      project_id: project, protocol_version: 'v3', statuses, after, limit });
    const result = response?.result ?? response ?? {};
    const batch = Array.isArray(result.items) ? result.items : [];
    items.push(...batch);
    // `matched_total` is a REMAINDER, not a total: the server counts deliveries
    // with delivery_seq > after, so the last page of a 101-match walk answers 1.
    // Only the first page can state the size of the match set (plus the seq the
    // walk started past). Verified read-only against the live API 2026-11-05:
    // after=0 -> 91, after=2 -> 89.
    if (!started) {
      matched_total = (Number.isInteger(result.matched_total) ? result.matched_total : items.length) + after;
      started = true;
    }
    next_after = result.next_after ?? next_after;
    more = result.has_more === true && batch.length > 0;
    if (!more) break;
    after = next_after;
  }
  const newest = [...items].sort((a, b) => sequence(b) - sequence(a)).slice(0, keep);
  return { result: { project_id: project, items: newest, matched_total,
    has_more: matched_total > newest.length, next_after,
    scanned: items.length, scan_complete: !more } };
}

/** One bounded, self-describing view of a `list_director_messages` page.
 *
 *  Per item: `message_id`, `subject`, `sender_project_id`, `created_at`, delivery
 *  `status`, `delivery_id`, `delivery_seq` and `version` (exactly what
 *  acknowledge_director_message needs - so the director can settle a delivery from
 *  the payload it was given - and the cursor to re-read that delivery's page),
 *  cheap ack truth, `body_chars`, and a body excerpt of at most `excerpt`
 *  characters (`excerpt` is 240: a few hundred is enough to recognise a message,
 *  and it is under the 320 the server itself uses for standing guidance).
 *  A delivery this project has already acknowledged (status acknowledged, or
 *  `acked_by_me`) is settled - it arrives as identity only, never a body. When a
 *  later message in the same thread is on the page, the earlier one is superseded
 *  and carries a shorter excerpt (`superseded_excerpt`).
 *
 *  The whole payload is capped at `ceiling` characters (JSON). The ceiling is
 *  enforced by shrinking every excerpt together (so a message that just arrived
 *  still has text, rather than losing a lottery), then by withholding bodies, and
 *  only then by omitting the oldest items and counting them; it never changes what
 *  was counted as matched.
 *
 *  @param page - the `list_director_messages` response (`{result}` or the result).
 *  @returns {{page: object, items: object[]}} never larger than `ceiling` chars.
 */
export function projectInboxDigest(page, {
  excerpt = INBOX_EXCERPT_CHARS, superseded_excerpt = INBOX_SUPERSEDED_EXCERPT_CHARS,
  subject_chars = INBOX_SUBJECT_CHARS, ceiling = INBOX_CEILING_CHARS,
} = {}) {
  const result = page?.result ?? page ?? {};
  const items = Array.isArray(result.items) ? result.items : [];
  const matched_total = Number.isInteger(result.matched_total) ? result.matched_total : items.length;
  const latest = new Map();
  for (const item of items) {
    const thread = item?.message?.thread_id;
    if (!thread) continue;
    const prior = latest.get(thread);
    if (!prior || sequence(item) > prior.seq) latest.set(thread, { seq: sequence(item), message_id: item.message?.message_id ?? null });
  }
  const perThread = new Map();
  for (const item of items) {
    const thread = item?.message?.thread_id;
    if (thread) perThread.set(thread, (perThread.get(thread) ?? 0) + 1);
  }
  // Newest first: the page arrives oldest-first (ORDER BY delivery_seq), so the
  // first thing a director reads today is seven-week-old news. Newest first also
  // means a ceiling that has to omit something omits the oldest.
  const records = items.map((item, index) => ({ item, index }))
    .sort((a, b) => sequence(b.item) - sequence(a.item) || a.index - b.index)
    .map(({ item }) => {
      const message = item?.message ?? {}, delivery = item?.delivery ?? {};
      const thread = message.thread_id ?? null, seq = sequence(item);
      const newer = thread ? latest.get(thread) : undefined;
      return { message, delivery, thread, seq, body: inboxText(message.body),
        status: delivery.status ?? null, acked: item?.acked_by_me === true,
        thread_size: thread ? perThread.get(thread) ?? 1 : 0,
        superseded_by: newer && newer.seq > seq ? newer.message_id : null };
    });
  const settled = record => record.status === 'acknowledged' || record.acked;
  const capOf = (record, excerptLen) => settled(record) ? 0
    : record.superseded_by ? Math.min(excerptLen, superseded_excerpt) : excerptLen;
  const project = (record, excerptLen) => {
    const cap = capOf(record, excerptLen);
    const identity = {
      message_id: record.message.message_id ?? null,
      subject: clip(record.message.subject, subject_chars),
      sender_project_id: record.message.sender_project_id ?? null,
      created_at: record.message.created_at ?? null,
      status: record.status,
      acked_by_me: record.acked,
      delivery_id: record.delivery.delivery_id ?? null,
      delivery_seq: record.seq >= 0 ? record.seq : null,
      version: record.delivery.version ?? null,
      body_chars: record.body.length,
      ...(record.thread_size > 1 ? { thread_id: record.thread } : {}),
      ...(record.superseded_by ? { superseded_by: record.superseded_by } : {}),
    };
    if (settled(record)) return { ...identity, ...(record.body.length ? { body_withheld: 'acknowledged' } : {}) };
    if (cap <= 0) return { ...identity, ...(record.body.length ? { body_withheld: 'ceiling' } : {}) };
    return { ...identity, ...(record.body.length
      ? { body_excerpt: clip(record.body, cap), ...(record.body.length > cap ? { excerpt_truncated: true } : {}) }
      : {}) };
  };
  // A page that only saw part of the matches must say so; `listProjectInbox` sets
  // these, a caller handing in one raw page means "this page is what I scanned".
  const scanned = Number.isInteger(result.scanned) ? result.scanned : items.length;
  const scan_complete = typeof result.scan_complete === 'boolean' ? result.scan_complete : result.has_more !== true;
  // A truncated scan cannot claim the newest of the match set - it only saw a slice
  // of it - so its window names the slice it saw and what it never reached.
  const assemble = (carried, excerptLen, omitted = 0) => {
    const projected = carried.map(record => project(record, excerptLen));
    return {
      page: {
        project_id: result.project_id ?? null,
        order: 'newest_first',
        window: scan_complete ? `newest ${projected.length} of ${matched_total}`
          : `newest ${projected.length} of the ${scanned} scanned (${matched_total} matched, ${Math.max(0, matched_total - scanned)} unscanned)`,
        items: projected.length,
        matched_total,
        scanned,
        scan_complete,
        has_more: matched_total > projected.length,
        next_after: result.next_after ?? null,
        items_omitted: omitted,
        bodies_withheld: projected.filter(item => item.body_withheld).length,
        excerpt_chars: excerptLen,
        superseded_excerpt_chars: superseded_excerpt,
        ceiling_chars: ceiling,
      },
      items: projected,
    };
  };
  const chars = digest => JSON.stringify(digest).length;
  let digest = assemble(records, excerpt);
  if (chars(digest) > ceiling) {
    // Over budget: shrink every excerpt together, down to the superseded length.
    // Withholding bodies first would leave whichever items the budget reached with
    // text and the rest with none - the newest message must not be the loser.
    for (let len = Math.floor((excerpt - 1) / 8) * 8; chars(digest) > ceiling && len >= superseded_excerpt; len -= 8) {
      digest = assemble(records, len);
    }
    if (chars(digest) > ceiling) digest = assemble(records, 0);
    for (let keep = records.length - 1; chars(digest) > ceiling && keep >= 0; keep--) {
      digest = assemble(records.slice(0, keep), 0, records.length - keep);
    }
  }
  return digest;
}

// ---- The director's pieces, re-exported with this deployment's labels bound ----
/** The environment variable core/dsh_driver.py sets on every driver process. */
export const SESSION_ENV = 'AITELIER_DSH_SESSION';
export const { NOTHING_LEFT, MAX_AUTO_CONTINUE, processAlive, watchRefusal, DORMANT_MIN_MINUTES,
  DORMANT_MAX_MINUTES, wakeAfterRefusal, pruneDepartedChildren, LiveChildren, phaseReport, continuationWaitKey,
  DORMANT_COMPACT_MIN_TOKENS, IdleReview, awaitingFinish, batchesToRedispatch, batchRedispatchRows, todoSummary,
  unfinishedWithoutChild, rebindRefusal, grepHistory } = core;
/** Why idle_dormant must refuse, with this deployment's wording for held work (State claims). */
export const dormancyRefusal = options => core.dormancyRefusal({ heldReason: texts.heldWork, ...options });
/** One durable snapshot per session bootstrap / successful compaction epoch. */
export class StateInjection extends core.ContextInjection {
  constructor(session, runtime, createMessage) {
    super(session, runtime, createMessage, { kind: labels.context, section: labels.contextSection, header: texts.contextHeader });
  }
}
export const connectionErrorMessage = (error, source = 'AItelier event subscription') => core.connectionErrorMessage(error, source);
/** user/message sources that are input to the director: the owner, this runtime's
 *  State/inbox/handback events, and native child messages. */
export const wakeSources = core.wakeSourcesFor(labels);
/** Whether a root session event ends dormancy. */
export const endsDormancy = core.endsDormancyFor(wakeSources);
export class Continuation extends core.Continuation {
  constructor(options = {}) { super({ endsDormancy, ...options }); }
}
/** The AItelier driver session a process belongs to (its environment), or undefined. */
export const processSession = (pid, procRoot = '/proc') => core.processSession(pid, procRoot, SESSION_ENV);
export const backgroundProcesses = (session, selfPid = process.pid, procRoot = '/proc') =>
  core.backgroundProcesses(session, selfPid, procRoot, SESSION_ENV);
/** The todo plan store through `python -m dsh_todo` (plugin step 3), its state directory in DSH_TODO_STATE_DIR. */
export class TodoCli extends core.TodoCli {
  constructor({ python, cwd, stateDir, module = 'dsh_todo', stateDirEnv = 'DSH_TODO_STATE_DIR' }) {
    super({ python, cwd, stateDir, module, stateDirEnv });
  }
}

/** Attempt statuses State can store, split by whether the attempt is still work.
 *
 *  A plan file is named after its State attempt, so a plan's unfinished items are
 *  work only while that attempt is live. `candidate`, `failed`, `superseded` and
 *  `abandoned` are settled: whatever is still open in the plan has already been
 *  accounted for by the attempt's terminal report, and nothing may run it again.
 *
 *  Both lists are ALLOWLISTS on purpose. The settled one decides what may stop
 *  blocking, so a status this runtime does not know - including one State adds
 *  later - is settled by neither list and keeps its plan blocking until this file
 *  is changed on review. "I could not classify it" is not "it settled". */
export const ACTIVE_ATTEMPT_STATUSES = new Set(['reserved', 'launching', 'running', 'paused', 'unknown']);
export const SETTLED_ATTEMPT_STATUSES = new Set(['candidate', 'failed', 'superseded', 'abandoned']);

/** The status a State `get_attempt` answer carries, or undefined when it carries
 *  none this runtime can read. The read returns the attempt envelope itself;
 *  `result` is tolerated the same way `idle_dormant`'s list_claims tolerates it. */
export function attemptStatus(answer) {
  const status = answer && typeof answer === 'object' ? (answer.status ?? answer.result?.status) : undefined;
  return typeof status === 'string' ? status : undefined;
}

/** Whether a State `get_attempt` answer proves the attempt has settled. A status in
 *  both lists (a later mistake) is live: blocking is the safe side of that typo. */
export function attemptSettled(answer) {
  const status = attemptStatus(answer);
  return status !== undefined && SETTLED_ATTEMPT_STATUSES.has(status) && !ACTIVE_ATTEMPT_STATUSES.has(status);
}

/** A State attempt answer as a directorHost work state. Settled exactly when
 *  `attemptSettled` says so (candidate -> completed, failed -> failed, superseded /
 *  abandoned -> cancelled); a live status is `working`; anything else `unknown`. */
export function attemptWorkState(answer) {
  const status = attemptStatus(answer);
  if (attemptSettled(answer)) return status === 'candidate' ? 'completed' : status === 'failed' ? 'failed' : 'cancelled';
  return status !== undefined && ACTIVE_ATTEMPT_STATUSES.has(status) ? 'working' : 'unknown';
}

/** The plan attempt ids whose State record says the attempt has SETTLED, read with
 *  one `get_attempt` per attempt through `read` (fails closed; see core.settledAttempts). */
export const settledAttempts = (plans, read, signal) => core.settledAttempts(plans,
  (id, s) => read('/api/state/query/get_attempt', { attempt_id: id }, s).then(attemptSettled), signal);

// Pure transport/state controller is separate so the actual wait/refresh logic
// is exercised without making model calls.
export class DriverRuntime {
  constructor({ project, driver, directory, request, onEvent = () => {}, onConnectionError = () => {}, onConnectionRestored = () => {}, retryDelayMs = 1000, todoPlans = async () => [], batchRedispatch = async () => [] }) {
    Object.assign(this, { project, driver, directory, request, onEvent, onConnectionError, onConnectionRestored, retryDelayMs, todoPlans, batchRedispatch });
    this.after = 0;
    this.inboxAfter = 0;
    this.snapshot = null;
    this.dirty = true;
    this.controller = new AbortController();
    this.lastHeartbeat = 0;
    this.connectionLost = false;
  }
  async init() {
    const me = await this.request('/api/drivers/me');
    if (me.driver_id !== this.driver) throw new Error('DSH driver identity mismatch');
    try {
      const saved = JSON.parse(await readFile(join(this.directory, 'cursor.json'), 'utf8'));
      this.after = saved.after; this.inboxAfter = saved.inboxAfter;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await this.refresh();
    // A new driver needs the current pending inbox, even if it predates launch.
    // State history starts at the bootstrap snapshot; future work is replayable.
    if (!this.after) this.after = this.snapshot.event_seq;
  }
  async refresh() {
    this.snapshot = await this.request(`/api/dsh-drivers/${encodeURIComponent(this.project)}/context`);
    // Plans are an addition to the snapshot; a broken todo CLI must not stop
    // the director from reading State.
    try { this.todo = await this.todoPlans(); this.redispatch = await this.batchRedispatch(); this.todoError = undefined; }
    catch (error) { this.todo = []; this.redispatch = []; this.todoError = String(error.message ?? error).slice(0, 1000); }
    this.dirty = false;
    return this.snapshot;
  }
  render() {
    // node_counts / nodes_omitted exist only on a route that omits VERIFIED nodes;
    // an older payload renders exactly as before.
    const omission = {
      ...(this.snapshot.node_counts ? { node_counts: this.snapshot.node_counts } : {}),
      ...(this.snapshot.nodes_omitted ? { nodes_omitted: this.snapshot.nodes_omitted } : {}),
    };
    return JSON.stringify({ project_id: this.project, driver_id: this.driver,
      nodes: this.snapshot.nodes, ...omission,
      ...(this.snapshot.issue_work?.length ? { issue_work: this.snapshot.issue_work } : {}),
      project_note: this.snapshot.project_note,
      private_note: this.snapshot.private_note, ...(this.todo?.length ? { todo: this.todo } : {}),
      ...(this.redispatch?.length ? { batch_redispatch: this.redispatch } : {}),
      ...(this.todoError ? { todo_error: this.todoError } : {}) });
  }
  async pump() {
    while (!this.controller.signal.aborted) {
      try {
        if (Date.now() - this.lastHeartbeat >= 20 * 60 * 1000) {
          await this.request(`/api/dsh-drivers/${encodeURIComponent(this.project)}/heartbeat`, {}, this.controller.signal);
          this.lastHeartbeat = Date.now();
        }
        const result = await this.request('/api/state/query/wait_for_state_change', {
          project_id: this.project, after: this.after, include_driver_inbox: true,
          // Node fetch's default response-header timeout is 300 seconds.
          inbox_after: this.inboxAfter, timeout_seconds: 240, return_when_idle: false,
        }, this.controller.signal);
        if (this.connectionLost) {
          await this.onConnectionRestored();
          this.connectionLost = false;
        }
        // No model turn on an ordinary timeout. Native State waits also maintain
        // presence while blocked; there is no independent heartbeat daemon.
        const inbox = (result.driver_inbox ?? []).filter(message => message.status !== 'resolved');
        if (!result.timed_out && (result.events?.length || inbox.length)) {
          this.dirty = true;
          await this.onEvent({ ...result, driver_inbox: inbox });
        }
        this.after = result.next_after ?? this.after;
        this.inboxAfter = result.next_inbox_after ?? this.inboxAfter;
        const file = join(this.directory, 'cursor.json');
        await writeFile(file + '.tmp', JSON.stringify({ after: this.after, inboxAfter: this.inboxAfter }), { mode: 0o600 });
        await rename(file + '.tmp', file);
      } catch (error) {
        const transient = error instanceof TypeError || error.name === 'TimeoutError' || error.status >= 500 || [408, 429].includes(error.status);
        if (this.controller.signal.aborted || !transient) throw error;
        // Transport recovery belongs to this subscription, not a model turn.
        this.connectionLost = true;
        await this.onConnectionError(error);
        await delay(this.retryDelayMs, undefined, { signal: this.controller.signal });
      }
    }
  }
  close() { this.controller.abort(new Error('Driver stopped')); }
}

/** One line per retried model call, on stderr. The driver's stderr is what the
 *  supervisor captures, and a retried completion is a second provider call: a
 *  silent retry reads as a hang and hides its own cost. */
export function modelRetryLog(line) {
  process.stderr.write(`AItelier DSH model retry: ${line}\n`);
}

/** The retry options this driver's model bridge runs with: the documented
 *  budget from the environment and a logger that actually writes. Exported so
 *  the witness exercises the object `boot` installs instead of re-deriving it —
 *  a test that built its own logger would stay green if boot dropped this one.
 *  `?? ''` keeps the read on the runtime's OWN env mapping: an unset key must
 *  not fall through to the helper's process.env default (which resolves to the
 *  180 s budget anyway). */
export function modelRetryOptions(env = process.env) {
  return { budgetMs: modelRetryBudgetMs(env.AITELIER_DSH_MODEL_RETRY_SEC ?? ''), log: modelRetryLog };
}

/** This deployment's directorHost (dsh-director host contract v1): the old boot()'s
 *  AItelier half, each piece called by the director at the point it always ran. */
export function aitelierHost(env = process.env, { request: suppliedRequest, runtime: suppliedRuntime } = {}) {
  const project = env.AITELIER_DSH_PROJECT;
  const hostExecution = env.AITELIER_DSH_EXECUTION === 'host';
  // A supplied request (tests) serves the State reads before install() runs; a
  // supplied, already initialised DriverRuntime (the conformance suite) lets context()
  // and events() run without the DSH-scoped install().
  let request = suppliedRequest, runtime = suppliedRuntime, schema, installModelSelection;
  return {
    version: '1.0.0',
    capabilities: ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels'],
    config: { sessionId: env.AITELIER_DSH_SESSION, provider: env.AITELIER_DSH_PROVIDER, model: env.AITELIER_DSH_MODEL,
      projectId: project, stateDir: env.AITELIER_DSH_STATE_DIR, dshEntry: env.AITELIER_DSH_ENTRY, rolesFile,
      sessionEnv: SESSION_ENV,
      todo: { python: env.AITELIER_TODO_PYTHON, cwd: env.AITELIER_TODO_CWD, module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' },
      watchIntervalMs: env.AITELIER_DSH_WATCH_INTERVAL_MS, dormantCompactMinTokens: env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS,
      dormantMinuteMs: env.AITELIER_DSH_DORMANT_MINUTE_MS, idleReviewMs: env.AITELIER_DSH_IDLE_REVIEW_MS,
      permissionPreset: hostExecution ? 'danger-full-access' : undefined },
    labels,
    texts,
    /** The effective table: roles.json plus AItelier's State additions. */
    roles: table => aitelierRoles(table),
    /** Host-scoped: the completion bridge, the provider key, project_work, the State runtime. */
    async install({ ctx, defineTool, llm, roles, todoPlans, batchRedispatch }) {
      const require = createRequire(env.AITELIER_DSH_ENTRY);
      ({ installModelSelection } = await import(require.resolve('@deepseek-ai/dsh-agent')));
      const token = (await readFile(env.AITELIER_DRIVER_TOKEN_FILE, 'utf8')).trim();
      request = suppliedRequest ?? (async (path, body, signal) => {
        const response = await fetch(new URL(path, env.AITELIER_URL), {
          method: body === undefined ? 'GET' : 'POST',
          headers: { 'Content-Type': 'application/json', 'X-AItelier-Driver-Token': token },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: signal ?? AbortSignal.timeout(30000),
        });
        if (!response.ok) {
          const error = new Error(`AItelier ${path}: HTTP ${response.status}`);
          const detail = await response.json().catch(() => null);
          if (detail?.detail) error.message += ' ' + JSON.stringify(detail.detail).slice(0, 1200);
          error.status = response.status;
          throw error;
        }
        return response.json();
      });
      // Efforts from roles.json; AITELIER_DSH_ROOT_REASONING_EFFORT still overrides the
      // director (an invalid value: max, as before the table).
      installHostModel(ctx, llm, { request, project, session: env.AITELIER_DSH_SESSION,
        rootEffort: rootReasoningEffort(env.AITELIER_DSH_ROOT_REASONING_EFFORT ?? roles.director.reasoning_effort),
        workerEffort: roles.worker.reasoning_effort,
        retry: modelRetryOptions(env) });
      // Resolve the existing mounted secret file into DSH's private credential
      // store, never into the environment inherited by test/build subprocesses.
      const secretDir = env.AITELIER_SECRETS_DIR ?? '/run/aitelier-secrets';
      try {
        const key = env.AITELIER_DSH_PROVIDER === 'deepseek-official' ? (await readFile(join(secretDir, 'DEEPSEEK_API_KEY'), 'utf8')).trim() : '';
        if (key) await ctx.credentials.set('DEEPSEEK_API_KEY', key);
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      // Host-scoped tools are available to native children too. Resolve the actual
      // caller from ToolExecution, never from the root agent captured by setup.
      if (!hostExecution) ctx.tools.register(defineTool({ name: 'project_work',
        description: 'Full project coding for this driver AND native subagents. Requires your assigned State attempt_id and owner fence. open: independent owned workspace; read: path; write: path/content/create; edit: path/old_str/new_str; bash: command/timeout in the existing isolated executor; deliver: optional commit_message, applies tested changes to Source and commits only those paths; push: expected_head, ordinary push to Source origin/current branch. Supply action and arguments. Use this for repository code/tests/integration; native local bash remains for driver-local orchestration. No additional plan/approval gate.',
        parameters: { attempt_id: { type: 'string', required: true }, fence: { type: 'integer', required: true },
          action: { type: 'string', required: true, enum: ['open', 'read', 'write', 'edit', 'bash', 'deliver', 'push'] },
          arguments: { type: 'object', additionalProperties: true } },
        output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          const result = await request(`/api/dsh-drivers/${encodeURIComponent(project)}/work`, {
            ...args, session_id: exec.agent.session.id,
          }, exec.signal);
          if (result.error) throw new Error(result.error);
          return result;
        },
      }));
      if (!hostExecution) ctx.systemPrompt.section({ name: 'aitelier:project-work', order: 5, interpolate: false,
        text: `Project ${project}: project_work is available to the driver and all native subagents. Parent assignments must include the registered State attempt_id and current owner fence. Each caller gets its own owned worktree: open, then read/write/edit/bash there, verify, then deliver (commit_message when needed). Only deliver writes back to Source. push(expected_head) uses the project's actual origin without force. bash runs in the existing isolated test container; do not run repository tests in the production backend. Use native local tools for orchestration/inbox/history, not as a fallback for denied project work. No new planning gate is required.` });
      if (hostExecution) ctx.systemPrompt.section({ name: 'aitelier:host-work', order: 5, interpolate: false,
        text: `Project ${project}: you and your native DSH subagents run on the Linux host with danger-full-access and approval never. Use native file and bash tools directly for coding, Git fetch/merge/commit/push, Docker and installed agent CLIs. project_work is not mounted in this runtime. Existing history or notes describing workspace-write, read-only Git or missing Docker describe the previous container runtime. Read the current State source binding for the repository; the backend binding /app maps to host ${env.AITELIER_DSH_BACKEND_SOURCE ?? 'the AItelier deployment checkout'}, all other absolute bindings retain their host path. Each writer needs its own checkout/worktree, each run one controller. Run AItelier pytest only in disposable containers: docker run --rm --network none --cpus 2 --memory 2g, one worker, at most four concurrent; never directly on the host or in the production container. Use your own driver token for AItelier calls. Full access does not change project rules or grant permission to publish private source publicly.` });
      // A worker's State limits follow the generic worker prompt; the director never gets them.
      ctx.systemPrompt.section({ name: 'aitelier:worker-state', order: 12.5, interpolate: false, text: context => {
        const id = (context?.agent ?? context?.scope)?.session?.id;
        return id !== undefined && id !== env.AITELIER_DSH_SESSION ? workerStateSkill : '';
      } });
      runtime = new DriverRuntime({ project, driver: env.AITELIER_DRIVER_ID,
        directory: env.AITELIER_DSH_STATE_DIR, request, todoPlans, batchRedispatch });
      await runtime.init();
      schema = await request('/api/state/schema');
    },
    /** Director scope: the goal and protocol sections, model selection, the hidden
     *  waits, state_read and send_user_message - in their old order. */
    async setup(scope, subject, { ctx, defineTool, runtimeSection, contextHooks }) {
      scope.systemPrompt.section({ name: 'aitelier:driver-goal', order: 10, interpolate: false,
        text: () => driverGoalSection(ctx.goals, subject) });
      scope.systemPrompt.section({ name: 'aitelier:driver-protocol', order: 11, interpolate: false,
        text: () => runtime.snapshot.driver_skill ?? '' });
      runtimeSection();
      scope.systemPrompt.section({ name: 'aitelier:state-protocol', order: 12.5, interpolate: false, text: stateSkill });
      installModelSelection(scope, { current: { provider: env.AITELIER_DSH_PROVIDER,
        model: env.AITELIER_DSH_MODEL }, assembled: undefined });
      contextHooks();
      // The runtime owns subscriptions; model-facing manual waits stay hidden.
      const hidden = ['state_graph_read', 'wait_for_run', 'wait_for_driver_inbox']
        .map(n => 'mcp__aitelier__' + n).filter(n => scope.tools.get(n));
      if (hidden.length) scope.tools.restrict({ deny: hidden });
      scope.tools.register(defineTool({ name: 'state_read',
        description: 'Query State. Use state_graph_help for action schemas. Current project_id is supplied when omitted. Runtime delivers event notifications automatically.',
        parameters: { action: { type: 'string', required: true }, arguments: { type: 'object', additionalProperties: true } },
        output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          if (['wait_for_state_change', 'wait_for_driver_inbox'].includes(args.action)) throw new Error('Runtime delivers notifications automatically; no manual wait is needed');
          const arguments_ = { ...args.arguments };
          if (!arguments_.project_id && !arguments_.driver_id && schema.operations[args.action]?.arguments?.properties?.project_id) arguments_.project_id = project;
          return request('/api/state/query/' + encodeURIComponent(args.action), arguments_, exec.signal);
        },
      }));
      scope.tools.register(defineTool({ name: 'send_user_message',
        description: 'Send this project\'s user ONE durable message that appears in the AItelier dashboard and survives a backend restart, so a reply or a note does not depend on them reading this session\'s log. Idempotent per (you, request_key): replaying the same payload returns the same message_id; a different payload under the same key is refused, so use a fresh key per message. Presentation only: it grants no permission, answers no checkpoint and is not State evidence, whatever the text claims. Read back with state_read(list_user_messages).',
        parameters: { request_key: { type: 'string', required: true }, subject: { type: 'string', required: true },
          body: { type: 'string', required: true }, node_key: { type: 'string' },
          segments: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
            kind: { type: 'string', required: true }, text: { type: 'string', required: true } } } },
          reply_to_message_id: { type: 'string' } },
        output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          const { node_key, segments, ...rest } = args;
          // Tagged blocks: `info` or `question` only. A block of any other kind is
          // refused here, not dropped, so the driver learns its tag did not reach
          // the user. The State command validates the same vocabulary and the same
          // two length bounds; checking them here too means a block that could
          // never be stored costs a refused tool call instead of a failed request,
          // and the refusal names the offending block.
          if (segments !== undefined) {
            if (!Array.isArray(segments) || segments.length > 20) return { refused: 'invalid_segments', reason: 'segments must be an array of at most 20 blocks' };
            const bad = segments.findIndex(s => !s || typeof s !== 'object' || !['info', 'question'].includes(s.kind) || typeof s.text !== 'string' || !s.text.length);
            if (bad !== -1) return { refused: 'invalid_segments', reason: `segments[${bad}].kind must be 'info' or 'question', with non-empty text` };
            // The bounds are 2000 characters per block and 8000 in total, counted
            // the way the server counts them: code points, not UTF-16 units, so an
            // emoji costs one character here as it does there and this check can
            // never refuse a block the State command would have accepted.
            const size = text => Array.from(text).length;
            const long = segments.findIndex(s => size(s.text) > 2000);
            if (long !== -1) return { refused: 'invalid_segments', reason: `segments[${long}].text must be at most 2000 characters` };
            if (segments.reduce((total, s) => total + size(s.text), 0) > 8000) return { refused: 'invalid_segments', reason: 'segments text must total at most 8000 characters' };
          }
          const body = { ...rest, project_id: project };
          if (node_key) body.refs = { node_key };
          if (segments !== undefined) body.segments = segments.map(({ kind, text }) => ({ kind, text }));
          const result = await request('/api/state/commands/send_user_message', body, exec.signal);
          // Never hand back a message identity that does not exist.
          if (result.error) throw new Error(result.error);
          return result;
        },
      }));
    },
    /** The startup / post-compaction State snapshot. */
    async context() { await runtime.refresh(); return runtime.render(); },
    progress: body => request(`/api/dsh-drivers/${encodeURIComponent(project)}/activity`, body),
    /** One get_attempt per plan attempt (see settledAttempts). */
    async workState(id, signal) { return attemptWorkState(await request('/api/state/query/get_attempt', { attempt_id: id }, signal)); },
    /** This driver's live State claims (idle_dormant refuses while any exist). */
    async heldWork(signal) {
      const listed = await request('/api/state/query/list_claims', { project_id: project, driver_id: env.AITELIER_DRIVER_ID, limit: 100 },
        signal);
      return ((listed.result ?? listed).claims ?? []).filter(c => c.status === 'live' && c.driver_id === env.AITELIER_DRIVER_ID);
    },
    /** The pending inbox, the driver goal, then the State subscription. */
    async events({ ctx, agent, notify, eventMessage, setHostError, clearHostError, isDisposed }) {
      // Bounded and self-describing: the newest page of the matches, projected (see
      // listProjectInbox / projectInboxDigest). An injection is re-sent on every
      // director-message wake, so it must not grow with project history - and it must
      // be the page a message that just arrived is on, not the oldest one.
      const projectInbox = async () => projectInboxDigest(await listProjectInbox(request, project));
      runtime.onConnectionError = async error => {
        const message = connectionErrorMessage(error);
        ctx.logger.warn(message);
        await setHostError(message);
      };
      runtime.onConnectionRestored = () => clearHostError();
      runtime.onEvent = async result => {
        // Steering starts an idle agent or queues input at the next active step.
        // Queue into the durable DSH inbox before committing the State cursor. An
        // interrupted delivery is replayed, never silently skipped.
        const inbox = result.events?.some(e => e.event_type === 'director_message_received') ? await projectInbox() : undefined;
        // A tick that carries nothing is not worth a turn. Steering it still wakes
        // the driver (the message exists, so the harness reports pending input), and
        // a driver woken with an empty payload either spins or learns to
        // ignore wakes - both are worse than not being woken.
        if (!eventCarriesSomething({ ...result, project_inbox: inbox })) return;
        // Its own writes coming back: the director already knows; a turn on them is waste.
        if (ownEchoOnly({ ...result, project_inbox: inbox }, env.AITELIER_DRIVER_ID)) return;
        // The id is the payload's digest (plugin step 7): a replay from the same cursor that
        // returns the same batch is recognised and not delivered twice; a batch that grew
        // has a new id and is delivered. Stable across restarts: it depends on nothing local.
        const text = 'AItelier runtime event:\n' + JSON.stringify({ ...result, project_inbox: inbox });
        await notify(text, { id: 'state:' + createHash('sha256').update(text).digest('hex').slice(0, 40) });
      };
      const inbox = await projectInbox();
      // Native goal rounds start/continue the director; bootstrap is not a human
      // message and must not manufacture human authority for goal tools.
      const pendingInbox = inbox.items.length;
      let removeInboxHook = () => {};
      const deliverInbox = () => {
        removeInboxHook();
        agent.steer(eventMessage('Pending project inbox:\n' + JSON.stringify(inbox)));
      };
      // Legacy project messages must not win the first-turn race and strand an
      // armed goal inside a non-goal wait turn before its first round is admitted.
      if (pendingInbox) removeInboxHook = ctx.on('session/event', (session, event) => {
        if (session !== agent.session || event.type !== 'user/message' || event.data.source?.kind !== 'goal') return;
        removeInboxHook();
        // Session observers cannot append reentrantly to their own log.
        queueMicrotask(() => { if (!isDisposed()) ctx.agents.withoutInitiator(deliverInbox); });
      });
      const objective = driverGoal(project);
      let seeded;
      const goal = bindDriverGoal(ctx.goals, agent, objective, { onSeed: (after, before) => { seeded = { after, before }; } });
      if (seeded) {
        // goal.md is a non-human authorization channel: every applied rewrite of a
        // mandate leaves a durable, append-only trace (the headless host shows no
        // logger output). Digests are of the stored (trimmed) objectives, so one
        // record's new digest is the next record's old digest.
        //
        // The trace is a RECORD, not the mandate: goals.edit above already applied the
        // new objective, and this block only witnesses it. Treating a failed write as
        // fatal would invert that - boot() rejects, the call site calls appExit(1), and
        // systemd restart-loops the whole host (every resident driver, every live
        // session) because a directory was momentarily unwritable. So a failure here is
        // reported and swallowed; the absence of a record is then itself observable.
        const { after, before } = seeded;
        try {
          const sha = text => createHash('sha256').update(text).digest('hex');
          const record = { at: new Date().toISOString(), goal_id: after.id, old_revision: before.revision, new_revision: after.revision,
            old_objective_sha256: sha(before.objective), new_objective_sha256: sha(after.objective),
            rounds_started: after.roundsStarted, old_max_goal_rounds: before.maxGoalRounds, new_max_goal_rounds: after.maxGoalRounds,
            goal_md_sha256: sha(goalTemplate), goal_md_commit: await goalMdCommit(goalFile, { cwd: env.AITELIER_TODO_CWD }) };
          ctx.logger.info(`director goal ${after.id} re-seeded: ${JSON.stringify(record)}`);
          const written = await appendGoalSeedRecord(env.AITELIER_DSH_STATE_DIR, record);
          if (!written.written) ctx.logger.error(`director goal ${after.id} was re-seeded but its trace was NOT written (${written.error})`);
        } catch (error) {
          ctx.logger.error(`director goal ${after?.id} was re-seeded but its trace was NOT written: ${String(error).slice(0, 300)}`);
        }
      }
      await ctx.sessionPersistence.flush();
      if (pendingInbox && (goal?.phase !== 'active' || goal.activation !== 'armed')) deliverInbox();
      return { done: runtime.pump(), stop: () => runtime.close() };
    },
  };
}

export function apply(ctx) {
  // see dropIndexedGitConfig: the tool shell's secret-name filter leaves COUNT without KEY_0
  dropIndexedGitConfig(process.env);
  ctx.provide('directorHost', aitelierHost(process.env));
}

/**
 * Drop git's indexed config environment before anything is spawned.
 *
 * The host unit exports GIT_CONFIG_COUNT=1 plus GIT_CONFIG_KEY_0/GIT_CONFIG_VALUE_0 so git picks up
 * the credential helper. The tool shell applies a secret-name filter to the environment, and that
 * filter matches by NAME: it correctly removes AITELIER_DRIVER_TOKEN and friends, and it also removes
 * GIT_CONFIG_KEY_0 because the name contains KEY. What survives is COUNT=1 with no KEY_0, which is
 * not a partial configuration git can ignore - git refuses to parse the environment at all
 * ("error: missing config key GIT_CONFIG_KEY_0", exit 128) and every bare git command fails, which
 * looks like a Git problem rather than a harness one (iss-92653d46882441de).
 *
 * An indexed pair cannot survive a name filter by construction, so the fix is to hand git no partial
 * config: with all three gone, git reads ~/.gitconfig as usual and works. The credential helper is
 * not needed for this driver's own use - every successful push from this driver ran with the whole
 * GIT_CONFIG_* set unset.
 */
export function dropIndexedGitConfig(env) {
  for (const name of Object.keys(env)) {
    if (/^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$/.test(name)) delete env[name];
  }
  return env;
}
