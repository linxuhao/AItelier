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
 *  SESSION_ENV, aitelierHost, attemptWorkState and stateRequest (the State retry policy,
 *  exported so its test can drive it). exports.test.mjs pins both lists. */
import { declaresImage, installHostModel, modelRetryBudgetMs, rootReasoningEffort, routeInputCache, wakeTakesImages } from './model.mjs';
import { rolesFile } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { autoReplyBody, boundGroupsSection, catalogOps, confirmOps, forwardFinalText, mediaOps, openProposals, openThreads, OwnerQuestionsTimer, OWNER_QUESTION_EVENTS, recordAutoReply, relayAnswer,
  replyToLetter, stateWake, stewardHostTexts, stewardOps, syncConversation, textOf, turnWatcher, unansweredOwnerMessages, withAttachmentLines,
  mediaRootFrom, ingestDashboardAttachments, ingestDirectorAttachments, sendUserMessageTool, uploadBody, mediaLine, wakeMedia } from './steward-host.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { MEMORY_REQUEST_EVENTS, processMemoryRequests } from './memory-requests.mjs';
import { CHANNEL_LOGIN_EVENTS, processChannelLoginRequests } from './channel-login-requests.mjs';
import { SCHEDULE_EVENTS, ScheduleTimer, deleteSchedule, listSchedules, setSchedule } from './schedules.mjs';
import { appendGoalSeedRecord, bindDriverGoal, driverGoalSection, goalMdCommit } from './goal.mjs';
import { labels, texts, stateInjectionSource, runtimeEventSource, todoBriefSource, continueSource,
  askTheUser, continuationQuestion, reviewInstruction } from './aitelier-texts.mjs';
import * as core from '../dsh-director/lib/core.mjs';
import { exitCause, failureClass } from '../dsh-director/lib/exit-cause.mjs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { writeSync } from 'node:fs';
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
/** How many consecutive ticks a subscription may fail to DELIVER before the driver
 *  concludes it cannot serve and ends (iss-6df19ff31a3b449b). A single failed
 *  completion is a failed turn and must not cost this process; a delivery that
 *  fails this many times running is not a single failure. */
const DELIVERY_FAILURE_LIMIT = 3;

export class DriverRuntime {
  constructor({ project, driver, directory, request, onEvent = () => {}, onConnectionError = () => {}, onDeliveryError, onConnectionRestored = () => {}, retryDelayMs = 1000, todoPlans = async () => [], batchRedispatch = async () => [] }) {
    Object.assign(this, { project, driver, directory, request, onEvent, onConnectionError, onConnectionRestored, retryDelayMs, todoPlans, batchRedispatch });
    // A failed DELIVERY is reported through the host's own channel when it has one
    // (`onDeliveryError`), so a completion failure is not labelled a lost subscription.
    this.onDeliveryError = onDeliveryError;
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
    // Consecutive ticks this subscription could not DELIVER (see the inner catch).
    // One is a failed turn; this many running is a driver that cannot serve.
    let deliveryFailures = 0;
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
          try {
            await this.onEvent({ ...result, driver_inbox: inbox });
            deliveryFailures = 0;
          } catch (error) {
            // A failure while DELIVERING one tick is not a failure of the
            // subscription (iss-6df19ff31a3b449b): one failed completion is a failed
            // turn, already reported through the agent's own error path, and the
            // stream can still serve the next tick. So it is reported on the host's
            // error channel - the existing one - and the tick is NOT consumed: the
            // cursor stays where it was, so the same event is fetched again and
            // reaches the director under the SAME id. That is the at-least-once
            // delivery the conformance suite pins: a redelivery the director can
            // recognise instead of a silent loss, and instead of a retry loop that
            // releases the subscription for a failure it did not have.
            // Consecutive failures are bounded: past the limit this driver cannot
            // serve its stream, and the caller ends it with the class recorded.
            if (this.controller.signal.aborted) throw error;
            deliveryFailures += 1;
            this.connectionLost = true;
            await (this.onDeliveryError ?? this.onConnectionError)(error);
            if (deliveryFailures >= DELIVERY_FAILURE_LIMIT) throw error;
            await delay(this.retryDelayMs, undefined, { signal: this.controller.signal });
            continue;
          }
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
/** ask_owner's whole mapping onto State (plugin step 8): one send_user_message with
 *  ONE question block, block id = question id, request_key derived from it. */
function askOwnerRequest(project, { id, text, context }) {
  return { request_key: `ask-owner:${project}:${id}`, project_id: project, subject: 'Question for you',
    body: context ?? '', segments: [{ kind: 'question', id, text }] };
}

/** A State READ that misses its deadline is retried once with a longer budget. The backend
 *  answers every sync State request from one shared worker pool, so under load a single read can
 *  miss its 30 s deadline while the next one succeeds: on 2026-11-22 a one-slot State read took
 *  21-68 s while /health answered in 34 ms, and each miss exited the driver - 38 restarts in 90
 *  minutes, and no line named the read or its duration. A WRITE keeps its single attempt, because
 *  only State's own request_key replay knows whether a failed write landed, and a caller that
 *  supplied a signal keeps that deadline rather than the default one. */
export function stateRequest(send, { log = {}, retryMs = 90000 } = {}) {
  // Path, duration and error identity only: never a body, never a token.
  const note = (level, event, fields) => {
    const sink = log[level];
    if (typeof sink === 'function') sink(event + ' ' + JSON.stringify(fields));
  };
  return async function request(path, body, signal, { readAs } = {}) {
    const started = Date.now();
    try {
      return await send(path, body, signal, readAs);
    } catch (error) {
      const elapsed = Date.now() - started;
      note('warn', 'state request failed', { path, kind: body === undefined ? 'read' : 'write',
        ms: elapsed, name: String(error?.name ?? 'Error'), status: error?.status ?? null });
      const timedOut = error?.name === 'TimeoutError' || error?.name === 'AbortError';
      if (!timedOut || signal || body !== undefined) throw error;
      const retryStarted = Date.now();
      try {
        const answer = await send(path, body, AbortSignal.timeout(retryMs), readAs);
        note('info', 'state read retried after a timeout', { path, first_ms: elapsed,
          retry_ms: Date.now() - retryStarted });
        return answer;
      } catch (retryError) {
        note('warn', 'state read retry failed', { path, retry_ms: Date.now() - retryStarted,
          name: String(retryError?.name ?? 'Error'), status: retryError?.status ?? null });
        throw retryError;
      }
    }
  };
}

export function aitelierHost(env = process.env, { request: suppliedRequest, runtime: suppliedRuntime } = {}) {
  const project = env.AITELIER_DSH_PROJECT;
  const hostExecution = env.AITELIER_DSH_EXECUTION === 'host';
  // Plugin step 12: the backend starts the reserved steward project with this role. The
  // steward gets no full-access preset, no project work, no goal mandate and no State tools
  // beyond its own (the server enforces the same: steward_read_only).
  const steward = env.AITELIER_DSH_ROLE === 'steward';
  // A supplied request (tests) serves the State reads before install() runs; a
  // supplied, already initialised DriverRuntime (the conformance suite) lets context()
  // and events() run without the DSH-scoped install().
  let request = suppliedRequest, runtime = suppliedRuntime, schema, installModelSelection;
  // Step 23 (design-steward 7.7.6): whose turn the steward is in (the plugin's
  // DirectorApi.turnAudience, from install() on). Unknown reads as a group: a tool's State
  // request then carries the public-read header, which only narrows.
  let turnAudience = () => undefined;
  const groupTurn = () => steward && (turnAudience()?.kind ?? 'group') !== 'owner';
  // The request a TOOL makes: in a group turn it reads as an anonymous reader. The host's own
  // requests (memory sync, polling, the reminder clock, the snapshot, channels and groups) use
  // `request` and never carry the header.
  const toolRequest = (path, body, signal) => request(path, body, signal, groupTurn() ? { readAs: 'public' } : undefined);
  // Step 16: the steward's reminder clock (created in events(), refreshed by the tools).
  let scheduleTimer;
  // Step 22: the clock that pushes the directors' open questions to the owner's channel.
  let questionsTimer;
  // Step 19: the director's ownerMessageSent, kept from install() for attention.ask (the
  // steward's questions are its messages to its owner too).
  let mirrorOwnerMessage;
  // Step 24 (7.8.9): a group requester's confirmation text goes out through the plugin's group send.
  let sendToGroup;
  // Step 24.1a: the plugin's "this turn read outside content" (api.externalContent), kept from install().
  let externalContent = () => false;
  // Step 24.2 (design-steward 7.8.12): the media library, when the DSH host gave a media root
  // (core/dsh_driver.py _environment); without one there is no `media` capability. 24.2b
  // (7.8.12a "一个写者一个子目录"): the steward writes `<userId>/` (5 GiB), each director its own
  // `d-<driver_id>/` (1 GiB, limitsFor) - one writer per subdirectory.
  // gatewayToken is this driver's token (install() reads it): the gateway's and the staged
  // uploads' header. attachmentStore is DSH's attachment service (install()'s ctx).
  let mediaLog = () => {}, gatewayToken, attachmentStore = () => undefined;
  const mediaRoot = mediaRootFrom(env);
  const mediaUser = steward ? env.AITELIER_DSH_MEMORY_USER || 'owner'
    : /^[A-Za-z0-9_.-]+$/.test(env.AITELIER_DRIVER_ID ?? '') ? `d-${env.AITELIER_DRIVER_ID}` : undefined;
  // The voice length is measured with ffprobe: AITELIER_MEDIA_FFPROBE names it (default `ffprobe`
  // on PATH); one that is missing refuses every voice `unsupported` (fail closed).
  const library = mediaRoot && mediaUser ? createMediaLibrary({ root: mediaRoot, userId: mediaUser,
    ffprobe: env.AITELIER_MEDIA_FFPROBE || 'ffprobe', log: (event, fields) => mediaLog(event, fields) }) : undefined;
  // Step 24.2b (7.8.12a "路线能力"): which routes take images, read from the backend at most once
  // a minute (the model bridge asks on every step).
  let routeLog = () => {};
  const routeInput = routeInputCache(signal => request(`/api/dsh-drivers/${encodeURIComponent(project)}/route-input`,
    undefined, signal ? AbortSignal.any([signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000)), { log: line => routeLog(line) });
  // Step 23 group wake (iss-0d20ef5dd9a44cd0): the optional directorHost hook for a group
  // @-turn's images. The plugin picks this group's own kept refs (and the message's own files)
  // and calls `line`; the read/saveImage step is the same wakeMedia the owner's wake uses, so
  // there is one implementation of "bytes -> native block -> 原图已附 line". An entry past the
  // plugin's bound is rendered by mediaLine without attaching: its ref-only line keeps the
  // media_call hint. On a text-only route wakeMedia attaches nothing and the wake is one block.
  const groupWakeMedia = async () => {
    if (!library) return undefined;
    const routes = await routeInput();
    const imageRoute = wakeTakesImages(routes), declared = declaresImage(routes);
    const media = wakeMedia({ library, imageRoute, declared, attachments: attachmentStore(),
      log: line => mediaLog('group_wake_media', { detail: String(line).slice(0, 200) }) });
    return { line: (attachment, { attach = true } = {}) => attach
      ? media.line(attachment) : mediaLine(attachment, { imageRoute, declared }), images: media.images };
  };
  // Step 24.2b (7.8.12a "宿主入库"): one staged dashboard upload, streamed with this driver's token
  // (the backend answers only a delivery target of the message carrying it), and its ack.
  const fetchUpload = async (uploadId, signal) => uploadBody(await fetch(new URL(
    `/api/dsh-drivers/${encodeURIComponent(project)}/uploads/${encodeURIComponent(uploadId)}`, env.AITELIER_URL),
  { headers: { 'X-AItelier-Driver-Token': gatewayToken ?? '' }, signal }));
  const ackUpload = (uploadId, verdict) => request('/api/state/commands/ack_conversation_upload',
    { project_id: project, upload_id: uploadId, ...verdict });
  const gatewayFetch = (connId, url, signal) => fetch(`${String(env.AITELIER_STEWARD_GATEWAY_URL ?? 'http://127.0.0.1:4445/api/steward/mcp').replace(/\/+$/, '')}/${encodeURIComponent(connId)}/fetch?url=${encodeURIComponent(url)}`,
    { headers: { 'X-AItelier-Driver-Token': gatewayToken ?? '' }, signal: signal ?? AbortSignal.timeout(180000) });
  const host = {
    version: '1.0.0',
    capabilities: ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels', 'attention',
      ...(steward ? ['memory', 'portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog'] : []),
      ...(library ? ['media'] : [])],
    config: { sessionId: env.AITELIER_DSH_SESSION, provider: env.AITELIER_DSH_PROVIDER, model: env.AITELIER_DSH_MODEL,
      projectId: project, stateDir: env.AITELIER_DSH_STATE_DIR, dshEntry: env.AITELIER_DSH_ENTRY, rolesFile,
      sessionEnv: SESSION_ENV,
      todo: { python: env.AITELIER_TODO_PYTHON, cwd: env.AITELIER_TODO_CWD, module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' },
      watchIntervalMs: env.AITELIER_DSH_WATCH_INTERVAL_MS, dormantCompactMinTokens: env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS,
      // The same number core/dsh_driver.py writes into the profile for DSH (plugin step 9).
      ...(env.AITELIER_DSH_MAX_ACTIVE_SUBAGENTS ? { maxActiveSubagents: Number(env.AITELIER_DSH_MAX_ACTIVE_SUBAGENTS) } : {}),
      dormantMinuteMs: env.AITELIER_DSH_DORMANT_MINUTE_MS, idleReviewMs: env.AITELIER_DSH_IDLE_REVIEW_MS,
      permissionPreset: hostExecution && !steward ? 'danger-full-access' : undefined,
      // The owner's memory lives on the host, outside every container mount (design 3.3).
      ...(steward ? { rootRole: 'steward', memory: { userId: env.AITELIER_DSH_MEMORY_USER || 'owner',
        ...(env.AITELIER_DSH_MEMORY_ROOT ? { root: env.AITELIER_DSH_MEMORY_ROOT } : {}),
        // Step 21 (design-steward 3.8.1): the owner's time zone for search's dates and the
        // index's date words; the host unit's drop-in sets it (Europe/Paris), default UTC.
        ...(env.AITELIER_DSH_MEMORY_TZ ? { timeZone: env.AITELIER_DSH_MEMORY_TZ } : {}),
        // Step 21 (design-steward 3.8.2): the local embedding server, only when its url is set
        // (the drop-in 20-memory-vector.conf sets both); off otherwise.
        ...(env.AITELIER_DSH_MEMORY_VECTOR_URL ? { vector: { url: env.AITELIER_DSH_MEMORY_VECTOR_URL,
          model: env.AITELIER_DSH_MEMORY_VECTOR_MODEL } } : {}) } } : {}) },
    labels,
    // Step 23: a group wake attaches its own kept images here; a host without a media library
    // declares no hook and the director's wake stays one text block.
    ...(library ? { wakeMedia: groupWakeMedia } : {}),
    // The steward's held work is an unanswered owner message, not a State claim.
    texts: steward ? stewardHostTexts(texts) : texts,
    /** The effective table: roles.json plus AItelier's State additions. */
    roles: table => aitelierRoles(table),
    /** Host-scoped: the completion bridge, the provider key, project_work, the State runtime. */
    async install({ ctx, defineTool, llm, roles, todoPlans, batchRedispatch, ownerMessageSent, turnAudience: audienceOf,
      externalContent: external, groupSend }) {
      if (steward) mirrorOwnerMessage = ownerMessageSent;
      if (steward && typeof groupSend === 'function') sendToGroup = groupSend;
      if (typeof audienceOf === 'function') turnAudience = audienceOf;
      if (steward && external) externalContent = external;
      const require = createRequire(env.AITELIER_DSH_ENTRY);
      ({ installModelSelection } = await import(require.resolve('@deepseek-ai/dsh-agent')));
      const token = (await readFile(env.AITELIER_DRIVER_TOKEN_FILE, 'utf8')).trim();
      gatewayToken = token;
      // The library logs a mime and a size, never a name or a byte.
      mediaLog = (event, fields) => ctx.logger.info(event + ' ' + JSON.stringify(fields));
      attachmentStore = () => ctx.get('attachments');
      routeLog = line => ctx.logger.warn(line);
      /** One State request, exactly as the driver has always sent it. Split out of `request`
       *  below so a read that missed its deadline can be tried once more with a longer budget. */
      const sendRequest = async (path, body, signal, readAs) => {
        const response = await fetch(new URL(path, env.AITELIER_URL), {
          method: body === undefined ? 'GET' : 'POST',
          headers: { 'Content-Type': 'application/json', 'X-AItelier-Driver-Token': token,
            ...(readAs === 'public' ? { 'X-AItelier-Read-As': 'public' } : {}) },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: signal ?? AbortSignal.timeout(30000),
        });
        if (readAs === 'public') {
          // Step 23 (design-steward 7.7.6): every public-read request of a group turn is logged by
          // path, status and the answer's sha256 - never its text - so the anonymous answer can be
          // measured in production against an unauthenticated reader's.
          const bytes = Buffer.from(await response.arrayBuffer());
          ctx.logger.info('public-read ' + JSON.stringify({ path, status: response.status,
            sha256: createHash('sha256').update(bytes).digest('hex') }));
          let parsed = null;
          try { parsed = JSON.parse(bytes.toString('utf8')); } catch { parsed = null; }
          if (!response.ok) {
            const error = new Error(`AItelier ${path}: HTTP ${response.status}` + (parsed?.detail ? ' ' + JSON.stringify(parsed.detail).slice(0, 1200) : ''));
            error.status = response.status;
            throw error;
          }
          return parsed;
        }
        if (!response.ok) {
          const error = new Error(`AItelier ${path}: HTTP ${response.status}`);
          const detail = await response.json().catch(() => null);
          if (detail?.detail) error.message += ' ' + JSON.stringify(detail.detail).slice(0, 1200);
          error.status = response.status;
          throw error;
        }
        return response.json();
      };
      // A State READ that times out is retried once with a longer budget (stateRequest, above).
      // A caller that supplied its own signal, and every write, keep their single attempt.
      request = suppliedRequest ?? stateRequest(sendRequest,
        { log: { warn: line => ctx.logger.warn(line), info: line => ctx.logger.info(line) } });
      // Efforts from roles.json; AITELIER_DSH_ROOT_REASONING_EFFORT still overrides the
      // director (an invalid value: max, as before the table).
      installHostModel(ctx, llm, { request, project, session: env.AITELIER_DSH_SESSION,
        rootEffort: rootReasoningEffort(env.AITELIER_DSH_ROOT_REASONING_EFFORT ?? roles[steward ? 'steward' : 'director'].reasoning_effort),
        workerEffort: roles.worker.reasoning_effort,
        retry: modelRetryOptions(env), routeInput });
      // Resolve the existing mounted secret file into DSH's private credential
      // store, never into the environment inherited by test/build subprocesses.
      const secretDir = env.AITELIER_SECRETS_DIR ?? '/run/aitelier-secrets';
      try {
        const key = env.AITELIER_DSH_PROVIDER === 'deepseek-official' ? (await readFile(join(secretDir, 'DEEPSEEK_API_KEY'), 'utf8')).trim() : '';
        if (key) await ctx.credentials.set('DEEPSEEK_API_KEY', key);
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      // Host-scoped tools are available to native children too. Resolve the actual
      // caller from ToolExecution, never from the root agent captured by setup.
      if (!hostExecution && !steward) ctx.tools.register(defineTool({ name: 'project_work',
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
      if (!hostExecution && !steward) ctx.systemPrompt.section({ name: 'aitelier:project-work', order: 5, interpolate: false,
        text: `Project ${project}: project_work is available to the driver and all native subagents. Parent assignments must include the registered State attempt_id and current owner fence. Each caller gets its own owned worktree: open, then read/write/edit/bash there, verify, then deliver (commit_message when needed). Only deliver writes back to Source. push(expected_head) uses the project's actual origin without force. bash runs in the existing isolated test container; do not run repository tests in the production backend. Use native local tools for orchestration/inbox/history, not as a fallback for denied project work. No new planning gate is required.` });
      if (hostExecution && !steward) ctx.systemPrompt.section({ name: 'aitelier:host-work', order: 5, interpolate: false,
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
    async setup(scope, subject, { ctx, defineTool, runtimeSection, contextHooks, memory, ownerMessageSent }) {
      // The director's goal, its driver protocol and the State protocol are a director's
      // mandate and tools; the steward has neither (its prompt is steward.md).
      if (!steward) scope.systemPrompt.section({ name: 'aitelier:driver-goal', order: 10, interpolate: false,
        text: () => driverGoalSection(ctx.goals, subject) });
      if (!steward) scope.systemPrompt.section({ name: 'aitelier:driver-protocol', order: 11, interpolate: false,
        text: () => runtime.snapshot.driver_skill ?? '' });
      runtimeSection();
      if (!steward) scope.systemPrompt.section({ name: 'aitelier:state-protocol', order: 12.5, interpolate: false, text: stateSkill });
      installModelSelection(scope, { current: { provider: env.AITELIER_DSH_PROVIDER,
        model: env.AITELIER_DSH_MODEL }, assembled: undefined });
      contextHooks();
      // The runtime owns subscriptions; model-facing manual waits stay hidden.
      const hidden = ['state_graph_read', 'wait_for_run', 'wait_for_driver_inbox']
        .map(n => 'mcp__aitelier__' + n).filter(n => scope.tools.get(n));
      if (hidden.length) scope.tools.restrict({ deny: hidden });
      if (!steward) scope.tools.register(defineTool({ name: 'state_read',
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
      // The steward answers the letters directors send it, in its project inbox and its
      // private inbox (owner 2026-11-17); it never starts a private message itself.
      // Marking a letter read is portfolio_inbox_acknowledge for a project letter and
      // private_inbox_acknowledge for a private one (steward-host.mjs; the server allows
      // the steward acknowledgement only, in either box).
      if (steward) {
        const json = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
        const inbox = { type: 'string', required: true, enum: ['project', 'private'] };
        const me = env.AITELIER_DRIVER_ID;
        // Step 16: the steward's scheduled reminders (design §7.4). The backend computes every
        // time; these only read and change rows, and the clock is re-read after each change.
        const changed = result => { void scheduleTimer?.refresh(); return result; };
        const scheduleShape = { type: 'object', additionalProperties: true, description: '{"kind":"cron","expr":"0 9 * * 1-5"} (5-field cron: minute hour day month weekday) or {"kind":"once","at":"<ISO time>"}' };
        scope.tools.register(defineTool({ name: 'schedule_list',
          description: 'Your reminders: each one\'s id, version, title, instruction, schedule, timezone, whether it is on, next and last run, and who created or last changed it (your owner can change them too). A group reminder names its group (target_group_label). One a family member made in a group (created_via "group") shows its title, time and group, never its words: only your owner changes it, on the dashboard.',
          parameters: {}, output: json,
          execute: (_args, exec) => listSchedules(toolRequest, exec.signal).then(schedules => ({ schedules })),
        }));
        scope.tools.register(defineTool({ name: 'schedule_set',
          description: 'Create a reminder (no schedule_id), or change one (schedule_id with the expected_version schedule_list showed). When it is due you are woken with its instruction. schedule: a 5-field cron expression (at least 15 minutes apart) or a single future time; tz defaults to your owner\'s timezone. A reminder gives you no new permission: you still only answer, pass on, propose and tell your owner. target "family_group" (only when your owner asks for a reminder to a family group): the instruction is then the exact words posted into that group when due, at most 500 characters, not an instruction to you - you are not woken; group is the bound group\'s name, required when more than one group is bound (a group_required refusal names every bound group). A reminder is your owner\'s own unless they name a group (or say "to the group" while only one is bound). Refused no_group_bound when no group is bound, group_required when several are and none is named, withheld when the words name something the group may not see.',
          parameters: { schedule_id: { type: 'string' }, expected_version: { type: 'integer' }, title: { type: 'string' }, instruction: { type: 'string' },
            schedule: scheduleShape, tz: { type: 'string' }, enabled: { type: 'boolean' },
            target: { type: 'string', enum: ['owner', 'family_group'] }, group: { type: 'string' },
            channel: { type: 'string', enum: ['wechat', 'slack'] } }, output: json,
          execute: (args, exec) => setSchedule(toolRequest, args, exec.signal).then(changed),
        }));
        scope.tools.register(defineTool({ name: 'schedule_delete',
          description: 'Delete one reminder (schedule_id with the expected_version schedule_list showed).',
          parameters: { schedule_id: { type: 'string', required: true }, expected_version: { type: 'integer', required: true } }, output: json,
          execute: (args, exec) => deleteSchedule(toolRequest, args, exec.signal).then(changed),
        }));
        scope.tools.register(defineTool({ name: 'reply_to_director',
          description: 'Answer a letter a director sent you, back to its sender: inbox "project" for your project inbox (the letters a wake shows, portfolio_inbox; mark them read with portfolio_inbox_acknowledge), "private" for your private inbox (mark them read with private_inbox_acknowledge, in the answer or once you are done with it). delivery_id is that letter\'s delivery. You can only reply; to pass your owner\'s words on, use handoff.',
          parameters: { inbox, delivery_id: { type: 'string', required: true }, text: { type: 'string', required: true } }, output: json,
          execute: (args, exec) => replyToLetter({ request: toolRequest, project, me, inbox: args.inbox, deliveryId: args.delivery_id, text: args.text, signal: exec.signal,
            external: externalContent() }),
        }));
        // Step 22 (design-steward 7.6.5): the model decides only that a chat message answers a
        // pushed question; the backend reads the codes, copies the words and writes the receipt.
        // Owner turns only: a group turn's guard refuses it (GROUP_TOOLS), and its request
        // carries the public-read header, which refuses every step 22 write (step 23).
        scope.tools.register(defineTool({ name: 'relay_answer',
          description: 'Your owner answered a question your host pushed to their chat (a line starting with a code like Q12, or a message of codes alone): pass that message\'s id. Your host finds the codes, sends your owner\'s words unchanged to each director who asked, and confirms to your owner. A message that asks about a question (Q12 是什么意思？) is not an answer: do not relay it.',
          parameters: { owner_message_id: { type: 'string', required: true } }, output: json,
          execute: (args, exec) => relayAnswer({ request: toolRequest, project, ownerMessageId: args.owner_message_id, mirror: ownerMessageSent, signal: exec.signal }),
        }));
      }
      const userMessageTool = sendUserMessageTool({ steward, library });
      scope.tools.register(defineTool({ name: 'send_user_message',
        description: userMessageTool.description,
        parameters: userMessageTool.parameters,
        output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          const { node_key, segments, attachments, ...rest } = args;
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
          // Step 24.2b: a director's attached files go into its own library first; one refused
          // refuses the whole message (no text goes out without its files).
          // Without the parameter (the steward; a director with no library) they are refused,
          // never dropped: the text never goes out alone.
          let stored;
          if (attachments !== undefined && !(Array.isArray(attachments) && !attachments.length)) {
            if (!userMessageTool.attachments) return { refused: 'attachments_unsupported', reason: steward
              ? 'send a file to your owner with send_media' : 'this host has no media library' };
            const ingested = await ingestDirectorAttachments({ library, project, requestKey: args.request_key, paths: attachments, signal: exec.signal });
            if (ingested.refused) return ingested;
            stored = ingested.attachments;
          }
          const body = { ...rest, project_id: project };
          if (node_key) body.refs = { node_key };
          // A caller-supplied `id` is the citation key the State guide tells a
          // driver to set, and the service keeps it exactly as sent, so it is
          // forwarded; a block without one keeps today's body byte for byte
          // (the service assigns the id in that case), and an absent or null id
          // is never sent as a key of its own.
          if (segments !== undefined) body.segments = segments.map(({ kind, text, id }) => (id == null ? { kind, text } : { kind, text, id }));
          if (stored) body.attachments = stored.map(file => file.ref);
          const result = await (steward ? toolRequest : request)('/api/state/commands/send_user_message', body, exec.signal);
          // Never hand back a message identity that does not exist.
          if (result.error) throw new Error(result.error);
          // The steward's message also goes to the owner's chat channel it answers (or the
          // default one), in the background: the plugin sends it once and records it (step 19).
          if (steward) void ownerMessageSent((result.result ?? result).message_id, textOf(body), body.reply_to_message_id);
          // The steward's reply joins its owner's turn in memory (a failure is logged, the reply stands).
          if (steward && memory) await syncConversation({ request, project, me: env.AITELIER_DRIVER_ID, memory, signal: exec.signal })
            .catch(error => ctx.logger.warn('memory sync: ' + String(error?.message ?? error).slice(0, 300)));
          return stored ? { ...result, attachments: stored } : result;
        },
      }));
    },
    /** The startup / post-compaction State snapshot. */
    async context() {
      await runtime.refresh();
      if (!steward) return runtime.render();
      // The steward's snapshot also names its open proposals (plugin step 13): after a
      // compaction it must still know what its owner has yet to decide.
      const proposals = await openProposals(request).catch(error => ({ error: String(error?.message ?? error).slice(0, 300) }));
      // Step 23.1 (7.7.24 item 8, review P1): a group reminder names the group it goes to; the
      // snapshot carries titles, times and group names only - never list items or a reminder's words.
      const schedules = await listSchedules(request).then(rows => rows.filter(r => r.enabled)
        .map(r => ({ schedule_id: r.schedule_id, title: r.title, schedule: r.schedule, tz: r.tz, next_run_at: r.next_run_at,
          ...(r.target === 'family_group' ? { target: r.target, target_group: r.target_group_label ?? null } : {}),
          ...(r.created_via === 'group' ? { created_via: 'group' } : {}) })))
        .catch(error => ({ error: String(error?.message ?? error).slice(0, 300) }));
      // Director 602 defect 4: a failed read of the group names is said (boundGroupsSection), never dropped.
      const groups = await boundGroupsSection(request);
      // Design-steward 7.3 layer 2: what is open, from data, not from the model's memory.
      const threads = await openThreads({ request, project, me: env.AITELIER_DRIVER_ID, stateDir: env.AITELIER_DSH_STATE_DIR });
      return runtime.render() + '\n\nOpen threads:\n' + JSON.stringify(threads) + '\n\nYour open proposals:\n' + JSON.stringify(proposals)
        + '\n\nYour reminders (on):\n' + JSON.stringify(schedules)
        + groups;
    },
    progress: body => request(`/api/dsh-drivers/${encodeURIComponent(project)}/activity`, body),
    /** One get_attempt per plan attempt (see settledAttempts). */
    async workState(id, signal) { return attemptWorkState(await request('/api/state/query/get_attempt', { attempt_id: id }, signal)); },
    /** This driver's live State claims (idle_dormant refuses while any exist). */
    async heldWork(signal) {
      // The steward holds no claims; what it must not sleep on is its owner waiting for an answer.
      if (steward) return (await unansweredOwnerMessages({ request, project, me: env.AITELIER_DRIVER_ID, signal }))
        .map(m => ({ node_key: `owner message ${m.message_id}` }));
      const listed = await request('/api/state/query/list_claims', { project_id: project, driver_id: env.AITELIER_DRIVER_ID, limit: 100 },
        signal);
      return ((listed.result ?? listed).claims ?? []).filter(c => c.status === 'live' && c.driver_id === env.AITELIER_DRIVER_ID);
    },
    /** The director's questions to its owner (plugin step 8) on State's conversation:
     *  ask is send_user_message with ONE question block whose id is the question id
     *  (a thin wrapper: askOwnerRequest is the whole mapping, and the bytes it stores
     *  are pinned against a direct send_user_message by tests/unit); its request_key
     *  derives from the id, so a retry is State's own idempotent replay and another text
     *  under the same id is State's idempotency_conflict. close is
     *  withdraw_user_question; list is list_user_questions. The owner's answer arrives
     *  as their message through events(); the citation {message_id, segment_id =
     *  question id} is on that message (list_driver_messages / list_user_conversation),
     *  not projected into the event. Bound, stated: list and close read the newest 200
     *  questions of the project (list_user_questions has no cursor); an older one is
     *  missed by list and answers unknown_question to close. */
    attention: {
      async ask({ id, text, context }, signal) {
        const body = askOwnerRequest(project, { id, text, context });
        // Step 23 (7.7.5): a tool's request - in a group turn it carries the public-read header,
        // so a guard slip still cannot write.
        const result = await toolRequest('/api/state/commands/send_user_message', body, signal)
          .catch(error => ({ error: String(error?.message ?? error) }));
        if (result.error) return /idempotency_conflict/.test(result.error)
          ? { refused: 'question_id_reused', reason: `question ${id} already asks something else; give this question its own id` }
          : { refused: 'state_refused', reason: String(result.error).slice(0, 500) };
        // The steward's question is an unprompted message to its owner: it also goes to the
        // owner's default channel, once (a retried ask is the same message, not sent again).
        if (mirrorOwnerMessage) void mirrorOwnerMessage((result.result ?? result).message_id, textOf(body), undefined);
        return { asked: true, question_id: id, message_id: result.message_id };
      },
      async close({ id, reason }, signal) {
        const listed = await toolRequest('/api/state/query/list_user_questions', { project_id: project, status: 'all', limit: 200 }, signal);
        // Another director in the project may use the same id: the list carries no
        // sender, so try the matches newest first and let State say whose each is.
        const matches = ((listed.result ?? listed).questions ?? []).filter(q => q.segment_id === id);
        let answered = false, foreign = false;
        for (const question of matches) {
          const result = await toolRequest('/api/state/commands/withdraw_user_question', { request_key: `close-question:${project}:${question.message_id}:${id}`,
            project_id: project, message_id: question.message_id, segment_id: id, reason }, signal).catch(error => ({ error: String(error?.message ?? error) }));
          if (!result.error) return result.withdrawn ? { closed: true, question_id: id, message_id: question.message_id }
            : { closed: false, already_closed: true, question_id: id, message_id: question.message_id };
          if (/not_own_question/.test(result.error)) { foreign = true; continue; }
          if (/question_answered/.test(result.error)) { answered = true; continue; }
          // The key names this exact question: a conflict is an earlier close of it
          // with another reason, which succeeded. That inference rests on
          // withdraw_user_question's ordering in core/driver_inbox.py: _replay refuses a
          // differing payload, and _record runs only after the row is written, so a
          // refused close leaves no key behind. A same-reason retry is replayed by State
          // and answers {closed: true} again (host.d.ts: `closed` means "a withdrawal
          // exists").
          if (/idempotency_conflict/.test(result.error)) return { closed: false, already_closed: true, question_id: id, message_id: question.message_id };
          return { refused: 'state_refused', reason: String(result.error).slice(0, 500) };
        }
        if (answered) return { refused: 'question_answered', reason: `your owner already answered ${id}` };
        return { refused: foreign ? 'not_own_question' : 'unknown_question',
          reason: foreign ? `question ${id} in this project is another director's` : `no question ${id} among this project's newest 200 questions` };
      },
      async list(signal) {
        const listed = await toolRequest('/api/state/query/list_user_questions', { project_id: project, status: 'open', limit: 200 }, signal);
        const page = listed.result ?? listed;
        const open = (page.questions ?? []).map(q => ({ id: q.segment_id, text: q.text, message_id: q.message_id }));
        if (page.has_more) open.truncated = true;   // more than the newest 200 are open: said, not hidden
        return open;
      },
    },
    /** The pending inbox, the driver goal, then the State subscription. */
    async events({ ctx, agent, notify, eventMessage, setHostError, clearHostError, isDisposed, memory, ownerMessageSent, ownerChannelLogin, groupSend }) {
      // The steward's owner conversation goes into memory before each turn sees it.
      const sync = () => steward && memory ? syncConversation({ request, project, me: env.AITELIER_DRIVER_ID, memory })
        .catch(error => ctx.logger.warn('memory sync: ' + String(error?.message ?? error).slice(0, 300))) : undefined;
      // Plugin step 14: the owner's memory requests from the dashboard run here, in code.
      const ownerRequests = () => steward ? processMemoryRequests({ request, memory, log: line => ctx.logger.warn(line) })
        .catch(error => ctx.logger.warn('memory requests: ' + String(error?.message ?? error).slice(0, 300))) : undefined;
      // Step 19: the owner's requests for a new WeChat login QR, also run in code; the QR
      // itself goes only through the channel's status report.
      const loginRequests = () => steward && ownerChannelLogin ? processChannelLoginRequests({ request,
        login: channel => ownerChannelLogin(channel), log: line => ctx.logger.warn(line) })
        .catch(error => ctx.logger.warn('channel login requests: ' + String(error?.message ?? error).slice(0, 300))) : undefined;
      await sync();
      await ownerRequests();
      await loginRequests();
      // Step 16: the reminder clock. Each wake it gives is delivered once per (row, due time).
      if (steward) {
        // Step 25 C11: a family-group reminder goes out through the plugin's group send (the
        // bridge's queue), never through a wake; only what did not go out wakes the steward.
        scheduleTimer = new ScheduleTimer({ request, log: line => ctx.logger.warn(line),
          deliver: (text, id) => notify(text, { id }), sendGroup: typeof groupSend === 'function' ? groupSend : undefined });
        await scheduleTimer.refresh();
        // Step 22: the push clock; each look awaits its mirrors (design-steward 7.6.2).
        questionsTimer = new OwnerQuestionsTimer({ request, mirror: (id, body) => ownerMessageSent(id, body),
          log: line => ctx.logger.warn(line) }).start();
        void questionsTimer.tick();
      }
      // Every owner message gets an answer: when a turn ends with the owner still waiting,
      // the steward's last words go to them as the reply (owner 2026-11-17).
      if (steward) {
        // Each automatic reply is recorded (log line + auto-replies.jsonl in the state dir), and so
        // is one that was not sent because the last words were only system clock lines. The
        // recorder lives in steward-host.mjs so that branch is testable without a DSH boot.
        const record = sent => recordAutoReply({ sent, stateDir: env.AITELIER_DSH_STATE_DIR, log: ctx.logger, sync });
        // Only an owner turn's last words answer the owner; a group turn's go to its group
        // (the plugin, through the host's post_group_reply) - never here (step 23, 7.7.7).
        const watch = turnWatcher((text, input) => turnAudience()?.kind === 'owner' && void forwardFinalText({ request, project, me: env.AITELIER_DRIVER_ID, text, input })
          // An automatic reply is the steward's message too: it goes where the answered message came from.
          .then(sent => { if (sent.forwarded) void ownerMessageSent(sent.message_id, autoReplyBody(text), sent.reply_to); return record(sent); })
          .catch(error => ctx.logger.warn('auto-reply: ' + String(error?.message ?? error).slice(0, 300))));
        ctx.on('session/event', (session, event) => { if (session === agent.session) watch(event); });
      }
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
      // The same channel for a tick that could not be DELIVERED: the subscription is
      // healthy, one turn inside the delivery failed, and the driver keeps serving
      // (iss-6df19ff31a3b449b). Labelled apart so the dashboard does not report a
      // lost subscription for a failed completion.
      runtime.onDeliveryError = async error => {
        const message = connectionErrorMessage(error, 'AItelier event delivery');
        ctx.logger.warn(message);
        await setHostError(message);
      };
      runtime.onConnectionRestored = () => clearHostError();
      runtime.onEvent = async raw => {
        // The owner's memory requests are the runtime's, never a model turn: run them and
        // drop their events from what the steward is woken with.
        const memoryRequest = steward && raw.events?.some(e => MEMORY_REQUEST_EVENTS.has(e.event_type));
        if (memoryRequest) await ownerRequests();
        if (steward && raw.events?.some(e => CHANNEL_LOGIN_EVENTS.has(e.event_type))) await loginRequests();
        // Schedule changes re-arm the clock and are no model turn either (step 16).
        if (steward) void scheduleTimer?.refresh();
        // Step 22: a director's question asked, withdrawn or answered: the push clock looks now
        // (and once more after the backend's wait), no model turn (design-steward 7.6.2).
        const questionEvents = steward ? (raw.events ?? []).filter(e => OWNER_QUESTION_EVENTS.has(e.event_type)) : [];
        if (questionEvents.length) void questionsTimer?.changed({ asked: questionEvents.some(e => e.payload?.change === 'asked') });
        const quiet = steward ? new Set([...MEMORY_REQUEST_EVENTS, ...SCHEDULE_EVENTS, ...CHANNEL_LOGIN_EVENTS, ...OWNER_QUESTION_EVENTS]) : MEMORY_REQUEST_EVENTS;
        const result = steward && raw.events?.some(e => quiet.has(e.event_type))
          ? { ...raw, events: raw.events.filter(e => !quiet.has(e.event_type)) } : raw;
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
        await sync();
        // The id is the payload's digest (plugin step 7): a replay from the same cursor that
        // returns the same batch is recognised and not delivered twice; a batch that grew
        // has a new id and is delivered. Stable across restarts: it depends on nothing local
        // (stateWake: not on the attachment lines either).
        // Step 24.2 (7.8.12): each owner message's attachments as one line each, from its source row.
        // Step 24.2b (7.8.12a "宿主入库"): the owner's dashboard uploads are stored into this host's
        // library and acknowledged first ("先存后送"); a transient failure throws, so this tick is
        // not consumed and the next one tries again (the ingest key stores nothing twice). On an
        // image route the images become native blocks of the wake; otherwise, and for voice and
        // files, the lines say how to read them (a director's also name the library file).
        let shown = result, images = [];
        if (library) {
          const dashboard = await ingestDashboardAttachments({ inbox: result.driver_inbox, library,
            audience: steward ? 'owner' : `project:${project}`, fetchUpload, ack: ackUpload, log: line => ctx.logger.warn(line) });
          // The same intersection resolveModel declares (a mixed route gets the placeholder lines).
          const routes = await routeInput();
          const media = wakeMedia({ library, imageRoute: wakeTakesImages(routes), declared: declaresImage(routes), attachments: attachmentStore(),
            director: !steward, log: line => ctx.logger.warn(line) });
          shown = await withAttachmentLines(request, result, undefined, { dashboard, sources: steward, line: media.line });
          images = media.images;
        }
        const { text, id } = stateWake(result, inbox, shown);
        await notify(text, images.length ? { id, images } : { id });
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
      const goal = steward ? undefined : bindDriverGoal(ctx.goals, agent, objective, { onSeed: (after, before) => { seeded = { after, before }; } });
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
      // Step 24.2: the library's 30-day and total-size cleanup, once now and daily.
      library?.start();
      return { done: runtime.pump(), stop: () => { scheduleTimer?.close(); questionsTimer?.close(); library?.stop(); runtime.close(); } };
    },
  };
  // v1.2 (plugin step 12): the steward's owner-message check, portfolio and handoff; a
  // director's host does not have them (implemented-but-undeclared is refused).
  if (steward) Object.assign(host, stewardOps({ request: (...args) => toolRequest(...args), rawRequest: (...args) => request(...args),
    project, me: env.AITELIER_DRIVER_ID, stateDir: env.AITELIER_DSH_STATE_DIR, groupTurn, externalContent: () => externalContent() }),
  // Step 24.1a: the steward's own catalog (its connections through the backend's gateway);
  // 24.1b: and confirmations of outward actions, the text sent back to the requester.
  { catalog: catalogOps((...args) => request(...args), { confirm: confirmOps((...args) => request(...args), { project,
    mirror: (...args) => mirrorOwnerMessage?.(...args), groupSend: (...args) => sendToGroup ? sendToGroup(...args) : false }) }) },
  // Step 24.2: the media library behind storeMedia, media_call and send_media. 24.2b: an owner
  // turn's send_media is a steward message with the file (State, then the plugin's mirror).
  library ? { media: mediaOps({ request: (...args) => request(...args), library, gatewayFetch,
    owner: { request: (...args) => toolRequest(...args), project, mirror: (...args) => mirrorOwnerMessage?.(...args) } }) } : {});
  // Step 24.2b: a director's own library. It has no gateway (fetch refuses) and no group.
  else if (library) {
    const { postGroup: _group, ...media } = mediaOps({ request: (...args) => request(...args), library,
      gatewayFetch: async () => { throw new Error('a director has no media gateway'); } });
    host.media = media;
  }
  return host;
}

/** How much of a cause this runtime's own exit line carries, in characters. It is
 *  one journal line and it is read by a person: the message's first line, capped. */
const EXIT_LINE_LIMIT = 300;
// A cause can quote a token from the environment or a rejected request. The same
// shape the supervisor's own record redacts: the label stays, the value goes.
const EXIT_BEARER = /(\bbearer\s+)[A-Za-z0-9._~+/=-]{6,}/gi;
const EXIT_LABELLED = /(\b(?:token|secret|password|passwd|passphrase|api[_-]?key|apikey|authorization)\s*[=:]\s*["']?)[A-Za-z0-9._~+/:@=-]{6,}/gi;

/** `AItelier DSH exit: <kind>[: <first line of the cause>]`, one bounded line. */
function exitLine(kind, detail = '') {
  const first = String(detail ?? '').split('\n')[0].trim();
  const named = first
    ? `: ${first.replace(EXIT_BEARER, '$1<redacted>').replace(EXIT_LABELLED, '$1<redacted>')}`
    : '';
  return `AItelier DSH exit: ${kind}${named}`.slice(0, EXIT_LINE_LIMIT);
}

/** The first line of whatever was thrown, with its class when it has one. */
function causeOf(value) {
  return (value && typeof value === 'object' && typeof value.stack === 'string') ? value.stack : value;
}

/** The failure class worth naming before the cause, or '' when the cause already
 *  names it.
 *
 *  `causeOf` returns the stack, whose first line is `<name>: <message>`, and for a
 *  generic error `failureClass` returns exactly that `<name>` - so prepending it read
 *  `uncaught exception: Error: Error: kaboom` (iss-489d473442794c3c). The classes
 *  `failureClass` exists to add - `HTTP 500`, `connection-level ECONNRESET
 *  after_send=true`, `abort`, `malformed body` - are not what a stack's first line
 *  begins with, so they are kept: those say something the cause cannot.
 */
function informativeClass(error) {
  const name = failureClass(error);
  const first = String(causeOf(error) ?? '').split('\n')[0].trim();
  return first.startsWith(name) ? '' : name;
}

const diagnosed = new WeakSet();

/**
 * Name this process's own death on stderr, before it ends, exactly once per cause.
 *
 * Measured 2026-11-16/17 (iss-10bb7c78d636411d): two director runtimes exited with
 * code 1 after 7.1 h and 7.8 h of uptime, and the crashing incarnation appended 0
 * bytes to its runtime.log - so the supervisor had nothing to record and the death
 * had no cause anywhere. Node ends a process non-zero for three different reasons,
 * and only two of them leave a trace: an uncaught exception and an unhandled
 * rejection print a stack (which nothing here kept), while a deliberate `exit(1)`
 * says "stop" and nothing else. This names all three in one bounded,
 * credential-free line each, and changes nothing about how any of them ends:
 *
 *  - `uncaughtExceptionMonitor` is an OBSERVER: Node still prints its own stack and
 *    still exits 1. It fires for an unhandled rejection too - Node ends one as an
 *    uncaught exception and passes `origin: 'unhandledRejection'`, which is the only
 *    reason there is no listener on 'unhandledRejection': that would REPLACE Node's
 *    crash path, and a runtime that survives a rejection today must keep surviving it.
 *  - the line is written as soon as the cause is known, so it lands ABOVE Node's own
 *    report, which is flushed afterwards (measured: with both streams in one file, as
 *    the host spawns the runtime, the report is the last thing in it). That is the
 *    order to prefer: the stack is still there for a reader, and the cause - one line,
 *    capped, credentials redacted - does not depend on the report's length.
 *  - 'exit' covers the third shape, the deliberate non-zero exit and `process.exitCode`,
 *    which leave no trace at all. It is the only hook that runs once the event loop is
 *    gone, so it writes with `writeSync`, and it says nothing when a cause was already
 *    named above: one death is still one line.
 *
 *  A DELIBERATE exit carries a cause when the code that requested it recorded one
 *  (`exitCause()`, set by the plugin's single exit decision): the line then names the
 *  failure class - `HTTP 500`, `connection-level ECONNRESET after_send=true`, `abort` -
 *  because `exit code 1` alone cannot tell those apart, which is exactly what two
 *  resident drivers left behind on 2026-11-19 (iss-6df19ff31a3b449b). The same class
 *  is added to an uncaught failure and an unhandled rejection, whose first line
 *  (`TypeError: fetch failed`) names no status and no code either. In both places it
 *  is added only when it says something the cause's own first line does not: a
 *  generic error's class IS that line's first token (`Error: kaboom`), so naming it
 *  again read `uncaught exception: Error: Error: kaboom` - a line whose class is
 *  doubled, not named (iss-489d473442794c3c).
 *
 * Installed by `apply()`, so it covers the runtime process and nothing else - and it
 * is NOT exported, because this file's export surface is pinned and neither that pin
 * nor the suite's file list is this change's to move. A second call for the same
 * process is a no-op.
 */
function installExitDiagnosis(target = process) {
  if (diagnosed.has(target)) return false;
  diagnosed.add(target);
  let spoke = false;
  // A write that fails here must not become the process's last act: it is already over.
  const speak = (kind, detail) => {
    spoke = true;
    try { writeSync(2, exitLine(kind, detail) + '\n'); } catch { /* the process is ending */ }
  };
  target.on('uncaughtExceptionMonitor', (error, origin) => {
    if (!spoke) {
      const kind = origin === 'unhandledRejection' ? 'unhandled rejection' : 'uncaught exception';
      const named = informativeClass(error);
      speak(named ? `${kind}: ${named}` : kind, causeOf(error));
    }
  });
  target.on('exit', code => {
    if (spoke || !code) return;  // a clean exit says nothing, a named death says it once
    const recorded = exitCause();
    if (!recorded) { speak(`exit code ${code}`, ''); return; }
    const named = informativeClass(recorded.error);
    speak(named
      ? `exit code ${code}: ${named} (${recorded.where})`
      : `exit code ${code} (${recorded.where})`, causeOf(recorded.error));
  });
  return true;
}

export function apply(ctx) {
  // see dropIndexedGitConfig: the tool shell's secret-name filter leaves COUNT without KEY_0
  dropIndexedGitConfig(process.env);
  installExitDiagnosis(process);
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
