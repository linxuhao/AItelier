/** A resident State DAG driver composed over the pinned DSH base profile. */
import { historyPage } from './history.mjs';
import { installHostModel, modelRetryBudgetMs, rootReasoningEffort } from './model.mjs';
import { installToolDiscovery } from './tool-discovery.mjs';
import { appendGoalSeedRecord, bindDriverGoal, driverGoalSection, goalMdCommit } from './goal.mjs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile, rename, stat } from 'node:fs/promises';
import { readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'aitelier-state-driver';
export const inject = ['agents', 'sessions', 'sessionPersistence', 'sessionQuery', 'systemPrompt', 'tools', 'compaction', 'credentials', 'llm', 'goals', 'tokenMeter'];

const goalFile = fileURLToPath(new URL('./goal.md', import.meta.url));
const goalTemplate = await readFile(goalFile, 'utf8');
const runtimeSkill = await readFile(new URL('./driver.md', import.meta.url), 'utf8');
export const driverGoal = project => goalTemplate.replaceAll('{{project_id}}', project);
export const stateInjectionSource = 'aitelier-driver-state';
/** Source kind for a State runtime event. Deliberately NOT `user`: a routine
 *  tick is not the owner speaking, and labelling it `user` made a driver hunt for
 *  a message that was never sent. */
export const runtimeEventSource = 'aitelier-runtime-event';
/** Source kind for the one-line plan brief a bound child gets after compaction. */
export const todoBriefSource = 'aitelier-todo-brief';

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

/** One durable snapshot per session bootstrap / successful compaction epoch. */
export class StateInjection {
  constructor(session, runtime, createMessage) {
    Object.assign(this, { session, runtime, createMessage });
    this.epoch = 0;
    this.injected = undefined;
    this.queued = undefined;
    for (const event of session.snapshotEvents()) this.observe(event);
  }
  observe(event) {
    if (event.type === 'compaction/end' && !event.data.error) this.epoch = event.seq;
    if (event.type === 'user/message' && event.data.source?.kind === stateInjectionSource) {
      this.injected = event.data.source.compaction_seq;
      this.queued = undefined;
    }
  }
  async prepare() {
    if (this.injected === this.epoch) return;
    await this.runtime.refresh();
    const text = 'AItelier driver state — startup / post-compaction snapshot:\n' + this.runtime.render();
    const message = this.createMessage({ content: [{ type: 'text', text }], source: {
      kind: stateInjectionSource, form: 'snapshot', compaction_seq: this.epoch,
      sections: [{ name: 'aitelier:state', text }],
    } });
    this.queued = { epoch: this.epoch, message };
    return message;
  }
  async beforeRequest() {
    if (this.injected === this.epoch || this.queued?.epoch === this.epoch) return;
    // Overflow retries do not run pre-step again. Their initial step already
    // committed its system prompt; append the fresh postcompact snapshot now.
    const message = await this.prepare();
    this.session.append('user/message', message, { surfaceOp: 'append' });
  }
}
export function connectionErrorMessage(error, source = 'AItelier event subscription') {
  const cause = error.cause;
  return `${source}: ${error.message}${cause ? ` (${[cause.code, cause.message].filter(Boolean).join(': ')})` : ''}`.slice(0, 2000);
}

/** user/message sources that are input to the director: the owner, this runtime's
 *  State/inbox/handback events, and native child messages. */
export const wakeSources = new Set(['user', runtimeEventSource, 'agent-message', 'subagent-settled', 'child']);
/** Whether a root session event ends dormancy. DSH also appends its own user/messages
 *  (runtime context, compaction checkpoints, this runtime's State snapshot): not input. */
export const endsDormancy = event => event.type === 'user/message' && wakeSources.has(event.data?.source?.kind ?? 'user');

/** Source of the runtime's continuation question: not input, not a goal round, not a wake source. */
export const continueSource = 'aitelier-continue';
/** The marker a director answers with when nothing is actionable. */
export const NOTHING_LEFT = 'NOTHING_LEFT';
/** Continuation questions in a row without real input before the director is left idle. */
export const MAX_AUTO_CONTINUE = 5;
export const continuationQuestion = 'AItelier continuation check: you ended a turn. Before parking, look for more work you can do now, in parallel with anything running: ready frontier nodes, open issues you can close or advance, next steps you promised yourself, results you can collect. If there is one, do it now. If not, answer with one line that starts with NOTHING_LEFT: naming why each candidate is blocked. For a long background job you started, call watch_process with its pid instead of polling it.';
export const reviewInstruction = 'Recheck live State frontier (ready nodes), open issues, next steps you promised yourself, results you can collect, worker capacity and prior blockers. Continue independent authorized work; a blocked task does not block the whole project. If nothing is actionable, reply with one line that starts with NOTHING_LEFT: naming why each candidate is blocked; if nothing is in flight either, call idle_dormant with that reason instead.';

/** Decides, at the end of each root turn, whether to ask the director once more.
 *  A turn that called a tool and did not answer NOTHING_LEFT may have stopped
 *  with work left (a cheap model tends to end a round after one step), so it is
 *  asked; a turn without a tool call, or one that answered NOTHING_LEFT, is the
 *  second consecutive stop and goes idle. Real input (wake sources) restores the
 *  budget of MAX_AUTO_CONTINUE questions, which bounds a director that only polls.
 *
 *  While the director waits on something that will wake it (`waitKey` non-empty:
 *  its live children and watched jobs), one NOTHING_LEFT answer holds for that
 *  wait: later turns are not asked again until the key changes. */
export class Continuation {
  constructor({ maxAuto = MAX_AUTO_CONTINUE, waitKey = () => '' } = {}) {
    this.maxAuto = maxAuto;
    this.waitKey = waitKey;
    this.auto = 0;
    this.tools = 0;
    this.text = '';
    this.answeredWait = undefined;
  }
  observe(event) {
    if (event.type === 'tool/call') this.tools++;
    else if (event.type === 'assistant/message') this.text = (event.data?.message?.content ?? [])
      .filter(block => block.type === 'text').map(block => block.text ?? '').join('\n');
    else if (endsDormancy(event)) this.auto = 0;
  }
  /** At idle: true to ask once more, false to go idle. Always starts a new turn record. */
  turnEnded() {
    const answered = this.text.includes(NOTHING_LEFT);
    const worked = this.tools > 0 && !answered;
    const wait = this.waitKey();
    if (answered) this.answeredWait = wait;
    this.tools = 0;
    this.text = '';
    if (!worked || this.auto >= this.maxAuto) return false;
    if (wait && wait === this.answeredWait) return false;
    this.auto++;
    return true;
  }
}

/** The AItelier driver session a process belongs to (its environment), or undefined. */
export function processSession(pid, procRoot = '/proc') {
  try {
    const entry = readFileSync(join(procRoot, String(pid), 'environ'), 'latin1').split('\0').find(e => e.startsWith('AITELIER_DSH_SESSION='));
    return entry?.slice('AITELIER_DSH_SESSION='.length);
  } catch { return undefined; }
}
/** Whether a process exists and is not a zombie. */
export function processAlive(pid, procRoot = '/proc') {
  try { return readFileSync(join(procRoot, String(pid), 'stat'), 'latin1').replace(/^.*\) /s, '')[0] !== 'Z'; }
  catch { return false; }
}
/** Live processes other than the driver itself that carry this driver's session:
 *  jobs its shell started, detached or not (the environment survives reparenting). */
export function backgroundProcesses(session, selfPid = process.pid, procRoot = '/proc') {
  if (!session) return [];
  let names;
  try { names = readdirSync(procRoot); } catch { return []; }
  return names.filter(name => /^\d+$/.test(name) && Number(name) !== selfPid)
    .map(Number).filter(pid => processSession(pid, procRoot) === session && processAlive(pid, procRoot)).sort((a, b) => a - b);
}

/** Why watch_process must refuse, or null. Only a running job of THIS driver may be
 *  watched (its session in the environment), at most `max` at a time. */
export function watchRefusal({ pid, note, alive, session, mySession, watched = false, count = 0, max = 16 }) {
  if (!Number.isSafeInteger(pid) || pid <= 1 || !note) return { refused: 'invalid_arguments', reason: 'pid must be a process id and note must say what it is' };
  if (!alive) return { refused: 'not_running', reason: `process ${pid} is not running` };
  if (session !== mySession) return { refused: 'not_this_driver', reason: `process ${pid} was not started from this driver` };
  if (!watched && count >= max) return { refused: 'too_many_watches', reason: `at most ${max} watched jobs` };
  return null;
}

/** Why idle_dormant must refuse, or null. Dormancy must never strand work: a live
 *  native child, a live State claim of this driver, or an unfinished todo plan
 *  without a live child each refuse. claims/plans are omitted for the early check.
 *  `liveChildren` is the count the reason states; `childSessions` names them, so the
 *  refusal is actionable: a director can find the child and tell a live worker from
 *  an id whose lifecycle end edge was missed (iss-dc85aa2c80f2454d). A caller
 *  that knows only the count still refuses, it just cannot name the sessions.
 *  `isLive` rejects an id that is no longer a child at all; with sessions supplied
 *  the count is the number of ids that survive it, so a departed id neither
 *  refuses nor inflates the number. */
export function dormancyRefusal({ reason, liveChildren = 0, childSessions = [], claims = [], plans = [], background = [], isLive = () => true }) {
  if (!reason) return { refused: 'reason_required', reason: 'say why no work is available' };
  const given = childSessions ? [...childSessions] : [];
  const named = given.filter(isLive).sort();
  // Only a caller that cannot name its children falls back to its own count.
  const count = given.length ? named.length : liveChildren;
  if (count) {
    return { refused: 'live_children', reason: `${count} native child(ren) still running`,
      ...(named.length ? { live_children: named } : {}) };
  }
  if (claims.length) return { refused: 'live_claims', reason: 'you hold live State claims', node_keys: claims.map(c => c.node_key) };
  if (plans.length) return { refused: 'unfinished_plans', reason: 'finish, report or hand back these plans first', attempts: plans.map(p => p.attempt_id),
    // Which of them have nothing left to run: the director's next step there is
    // todo_finish, not another dispatch.
    ...(plans.some(p => p.ready_to_finish) ? { ready_to_finish: plans.filter(p => p.ready_to_finish).map(p => p.attempt_id) } : {}) };
  if (background.length) return { refused: 'background_processes', reason: 'jobs you started are still running; watch_process them or wait', pids: background };
  return null;
}

/** Drop from `live` every activation the agent registry can prove departed: its start
 *  event reported `local: true`, so it owns a local Agent here, and `isLive` now says
 *  no. A non-local activation owns no local Agent, so nothing in this process can prove
 *  it gone and it stays. Returns the pruned ids. */
export function pruneDepartedChildren(live, { isLocal = () => true, isLive = () => true } = {}) {
  const departed = [...live].filter(id => isLocal(id) && !isLive(id));
  for (const id of departed) live.delete(id);
  return departed;
}

/** The runtime's live native children: ONE set, and the ONE place that set is
 *  reconciled against the agent registry.
 *
 *  `sessions` is the identity every reader holds - a native child's session id, the
 *  same id `ctx.agents.get` answers for. `runs` keeps the runId -> session pairing
 *  `subagent/start` hands over, so the phase report can name an identity the registry
 *  answers for instead of a runId nothing can look up (iss-de1b4e7f644f4f0e). `local`
 *  names the ids whose start event said `local: true`: only those own a local Agent in
 *  this process, so only those can be proven gone. A non-local activation owns nothing
 *  here to look up, so its absence proves nothing and it stays.
 *
 *  `isLive` is the liveness oracle and the only one. The runtime passes
 *  `ctx.agents.get(id) !== undefined` - the call shape the idle_dormant path always
 *  used, with a missing registry proving nothing - and a test passes a registry that
 *  answers absent. `reconcile()` drops every entry that oracle proves departed from
 *  all three structures, is idempotent, and NEVER adds an entry back.
 *
 *  Reading reconciles: every accessor below runs `reconcile()` first, so no reader can
 *  see a departed child and a reader added later cannot forget the check
 *  (iss-5f2c09b74cb040ee). A child that ended without its `subagent/end` edge - a host
 *  restart, a killed worker - therefore stops refusing dormancy, stops keying the
 *  continuation wait, stops making a plan look bound to a live session and stops
 *  appearing in the phase report. A child the registry still answers for is untouched. */
export class LiveChildren {
  constructor({ isLive = () => true } = {}) {
    this.isLive = isLive;
    this.sessions = new Set();
    this.runs = new Map();
    this.local = new Set();
  }
  /** A child started: pair its runId with its session id, and remember whether it
   *  owns a local Agent here (both come from the one `subagent/start` payload).
   *  Pinned DSH publishes this edge only after a local child's agent is registered
   *  (dsh-subagent observeActivation -> observer.start after its handle exists), so
   *  the read that follows this call already finds it in the registry. */
  start({ id, runId, local = false } = {}) {
    this.sessions.add(id);
    if (runId !== undefined) this.runs.set(runId, id);
    if (local) this.local.add(id);
    return this;
  }
  /** The normal end edge - the only way out the set used to have. */
  end({ id, runId } = {}) {
    this.sessions.delete(id);
    if (runId !== undefined) this.runs.delete(runId);
    this.local.delete(id);
    return this;
  }
  /** Drop what the oracle proves departed; returns the dropped session ids. */
  reconcile() {
    const departed = pruneDepartedChildren(this.sessions, { isLocal: id => this.local.has(id), isLive: this.isLive });
    for (const id of departed) this.local.delete(id);
    // The runId pairing goes with the session: a runId whose child is gone is the
    // same leak in a second identity.
    for (const [runId, id] of this.runs) if (!this.sessions.has(id)) this.runs.delete(runId);
    return departed;
  }
  /** Reconciled count: the number dormancy's reason states. */
  size() { this.reconcile(); return this.sessions.size; }
  /** Reconciled membership: is this session id still a live child, and so still the
   *  session a plan stays bound to? */
  has(id) { this.reconcile(); return this.sessions.has(id); }
  /** Reconciled ids, sorted: what a reader may NAME (the phase report, the
   *  continuation key, the dormancy reason). */
  ids() { this.reconcile(); return [...this.sessions].sort(); }
  /** Reconciled ids as the Set the plan views take. */
  sessionSet() { return new Set(this.ids()); }
}

/** The phase report body: what this driver is doing. `subagents` names the live
 *  native child SESSIONS - the identity the agent registry can answer for - never
 *  runIds (iss-de1b4e7f644f4f0e). Its consumer counts the array (`running_subagents`
 *  in core/dsh_driver.py), and a runId cannot be looked up anywhere. */
export function phaseReport({ phase, children, error = '', event }) {
  return { phase, subagents: children.ids(), error, ...(event ? { event } : {}) };
}

/** What will wake the director: its live children and watched jobs, as one key.
 *  Reading `children` reconciles it, so a child that ended without its end edge
 *  leaves the key instead of holding a NOTHING_LEFT answer for a session that is
 *  gone (iss-5f2c09b74cb040ee). Synchronous by design: the oracle is a synchronous
 *  registry lookup, and `Continuation.turnEnded()` is synchronous. */
export function continuationWaitKey({ children, watched = [] }) {
  return [...children.ids(), ...watched.map(pid => 'pid:' + pid)].join(',');
}

/** Below this measured context (tokens) entering dormancy does not compact: a cold re-read is cheap. */
export const DORMANT_COMPACT_MIN_TOKENS = 32000;

/** The resident runtime owns idle time; the model never holds a wait tool open.
 *
 *  Dormancy (idle_dormant) parks only the timed review: nothing is stopped, the
 *  State/inbox subscription and child messages still steer the agent, and the
 *  first such message ends dormancy (wake). On the first idle after entering it,
 *  `compact` runs once - the provider cache will be cold by the next wake anyway,
 *  so a large context is cheapest to shrink while it is still warm. */
export class IdleReview {
  constructor({ agent, goals, notify, onError, compact, continuation, ask, canContinue = () => true, intervalMs = 15 * 60 * 1000 }) {
    Object.assign(this, { agent, goals, notify, onError, compact, continuation, ask, canContinue, intervalMs });
    this.closed = false;
  }
  enterDormancy(reason) {
    clearTimeout(this.timer);
    this.dormant = { reason, since: new Date().toISOString(), compactPending: true };
    return this.dormant;
  }
  /** Ends dormancy; returns what it was (undefined when not dormant). */
  wake() {
    const was = this.dormant;
    this.dormant = undefined;
    return was;
  }
  statusChanged(status) {
    clearTimeout(this.timer);
    if (this.closed || status !== 'idle') return;
    // Keep the durable mandate active, while parking immediate native rounds.
    // Incoming steering and native child messages still wake an idle agent.
    this.goals.disarm(this.agent);
    const again = this.continuation?.turnEnded() ?? false;
    if (this.dormant) {
      if (this.dormant.compactPending && this.compact) {
        this.dormant.compactPending = false;
        Promise.resolve().then(this.compact).catch(this.onError);
      }
      return;
    }
    if (this.goals.get(this.agent)?.phase !== 'active') return;
    if (again && this.ask && this.canContinue()) {
      Promise.resolve().then(this.ask).catch(this.onError);
      return;
    }
    this.timer = setTimeout(() => {
      if (this.closed || this.agent.status !== 'idle' || this.goals.get(this.agent)?.phase !== 'active') return;
      Promise.resolve().then(this.notify).catch(this.onError);
    }, this.intervalMs);
    this.timer.unref?.();
  }
  close() { this.closed = true; clearTimeout(this.timer); }
}

/** The host-side todo plan store, reached only through `python -m core.todo_cli`.
 *  Predicates, freezing and evaluation live in Python; this only shells out. */
export class TodoCli {
  constructor({ python, cwd, stateDir }) {
    Object.assign(this, { python, cwd, stateDir });
    this.store = join(stateDir, 'todo');
  }
  /** Exit 0 is a result, exit 2 a refusal; anything else is a broken call.
   *  Bounded: a hung predicate or interpreter must not hold a step forever. The
   *  CLI leads its own process group. On timeout or abort the group gets SIGTERM
   *  first, so the CLI can stop the predicates it runs in their own sessions (a
   *  SIGKILLed CLI cannot), then SIGKILL after 5 s: a predicate's test run or
   *  docker client must not outlive the call. */
  run(args, signal, timeout = 60000) {
    return new Promise((resolve, reject) => {
      const child = spawn(this.python, ['-m', 'core.todo_cli', ...args], { cwd: this.cwd, detached: true,
        stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, AITELIER_DSH_STATE_DIR: this.stateDir } });
      let stdout = '', stderr = '', killed, escalate;
      const signalGroup = name => { try { process.kill(-child.pid, name); } catch { /* ESRCH: the group is already gone */ } };
      const killGroup = why => {
        if (killed) return;
        killed = why;
        signalGroup('SIGTERM');
        escalate = setTimeout(() => signalGroup('SIGKILL'), 5000);
      };
      const timer = setTimeout(() => killGroup(`timed out after ${timeout} ms`), timeout);
      const onAbort = () => killGroup('aborted');
      signal?.addEventListener('abort', onAbort, { once: true });
      if (signal?.aborted) onAbort();
      child.stdout.on('data', chunk => { stdout += chunk; });
      child.stderr.on('data', chunk => { stderr += chunk; });
      const settle = () => { clearTimeout(timer); clearTimeout(escalate); signal?.removeEventListener('abort', onAbort); };
      child.on('error', error => { settle(); reject(new Error(`todo_cli ${args[0]}: ${error.message}`)); });
      child.on('close', (code, killSignal) => {
        settle();
        if (killed) return reject(new Error(`todo_cli ${args[0]} ${killed}; its process group was stopped (${killSignal ?? 'exit ' + code})`));
        if (code !== 0 && code !== 2) return reject(new Error(`todo_cli ${args[0]}: exit ${code ?? killSignal} ${stderr}`.slice(0, 2000)));
        try { resolve({ code, result: JSON.parse(stdout) }); }
        catch { reject(new Error(`todo_cli ${args[0]} printed no JSON (exit ${code}): ${stdout} ${stderr}`.slice(0, 2000))); }
      });
    });
  }
  /** The bound for a predicate-running call: the plan's own budget (seconds,
   *  from `status`) plus a minute for the CLI itself; 60 s when none is given. */
  async bound(attempt, budget, signal) {
    const { code, result } = await this.run(['status', '--attempt=' + attempt], signal);
    const seconds = code === 0 ? Number(budget(result)) : NaN;
    return Number.isFinite(seconds) && seconds > 0 ? (seconds + 60) * 1000 : 60000;
  }
  /** A tool result: a refusal is returned, not thrown, and always carries the brief. */
  async call(attempt, args, signal, timeout) {
    const { code, result } = await this.run(args, signal, timeout);
    const value = code === 2 ? { refused: true, ...result } : result;
    if (typeof value.brief === 'string') return value;
    const status = await this.run(['status', '--attempt=' + attempt], signal);
    return { ...value, brief: status.code === 0 ? status.result.brief : null };
  }
  /** Where `todo_finish` puts an attempt's candidate: the bundle and the report. */
  finishDir(attempt) { return join(this.store, 'finish', attempt); }
  /** The candidate report `todo_finish` writes. The plan file itself is NEVER
   *  rewritten by finishing, so this file's existence is the one record that
   *  finishing already ran for that attempt. */
  finishReport(attempt) { return join(this.finishDir(attempt), attempt + '-candidate.json'); }
  /** Every stored plan's summary (`list`), plus whether its candidate report
   *  exists. No store yet means no plan was ever made, so the interpreter is not
   *  started for it. */
  async plans(signal) {
    try { await stat(this.store); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    const { code, result } = await this.run(['list'], signal);
    if (code !== 0) throw new Error('todo_cli list refused: ' + JSON.stringify(result).slice(0, 1000));
    return Promise.all(result.plans.filter(plan => !plan.error).map(async plan =>
      ({ ...plan, finish_reported: await fileExists(this.finishReport(plan.attempt_id)) })));
  }
}

/** Whether a path exists; an unreadable one counts as absent (fail towards
 *  reminding the director, never towards silently dropping a plan). */
async function fileExists(path) {
  try { await stat(path); return true; } catch { return false; }
}

/** Whether a plan's every item has passed while no candidate was ever produced.
 *
 *  That is the state a host restart leaves behind when it kills a child right
 *  after its last `todo_done`: the plan has no `remaining` items, the State
 *  attempt is still live and has no candidate report, and `todo_finish` has not
 *  been run - so the only work left is the DIRECTOR's. Both views below surface
 *  such a plan, marked `ready_to_finish`, precisely because filtering on
 *  `remaining.length` alone dropped it out of them and nothing ever reminded the
 *  director that the attempt was ready to finish (iss-af4fdabe05954077).
 *
 *  `todo_finish` does not rewrite the plan file, so `remaining: []` alone cannot
 *  tell "nobody finished this" from "the candidate is already written". The
 *  report `todo_finish` writes is what says it ran (`finish_reported`, read in
 *  `TodoCli.plans`); when that is unknown the plan stays surfaced.
 *
 *  Nothing here re-opens the items: `remaining` is empty, so no item is offered
 *  as work again. */
export function awaitingFinish(plan) {
  return Array.isArray(plan.remaining) && plan.remaining.length === 0 && plan.finish_reported !== true;
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

/** The plan attempt ids whose State record says the attempt has SETTLED.
 *
 *  One `get_attempt` for every plan a view could show - one that still has
 *  items, and one whose items have all passed but which was never finished -
 *  through the same State read the rest of this runtime uses. FAILS CLOSED: a
 *  request that throws (State unreachable, an unknown attempt), an answer with
 *  no status, or a status that is not provably terminal leaves that attempt OUT
 *  of the set, so its plan keeps blocking exactly as it did before this existed.
 *  This set only ever shrinks the blocking list by what State actually said. */
export async function settledAttempts(plans, read, signal) {
  const settled = new Set();
  await Promise.all(plans.filter(plan => plan.remaining?.length || awaitingFinish(plan)).map(async plan => {
    let answer;
    try { answer = await read('/api/state/query/get_attempt', { attempt_id: plan.attempt_id }, signal); }
    catch { return; /* fail closed: an unread plan is unfinished work, not settled */ }
    if (attemptSettled(answer)) settled.add(plan.attempt_id);
  }));
  return settled;
}

/** The post-compaction snapshot's view of plans the director still owns: enough
 *  to resume, not the items. `settled` is `settledAttempts`' answer (absent means
 *  "nothing is known to have settled"), so a settled plan is not listed as
 *  awaiting work.
 *
 *  `ready_to_finish` is the explicit marker: true when every item passed and the
 *  candidate has not been produced, so the plan's `remaining_count` is 0 and the
 *  director's next step is `todo_finish` (with no item left to run); false when
 *  items remain. A plan with items left is never reported as ready to finish. */
export function todoSummary(plans, liveSessions, settled = new Set()) {
  return plans.filter(p => !settled.has(p.attempt_id) && (p.remaining?.length || awaitingFinish(p)))
    .map(p => ({ attempt_id: p.attempt_id, node_key: p.node_key, next: p.next,
      remaining_count: p.remaining?.length ?? 0, ready_to_finish: awaitingFinish(p),
      child_alive: liveSessions.has(p.child_session) }));
}

/** Plans the idle review must hand back: unfinished, their attempt not settled,
 *  and no live child on them. A plan whose attempt settled is history, not a
 *  blocker; a plan whose attempt State could not answer for stays one.
 *
 *  `ready_to_finish` says which of them have nothing left to run: every item
 *  passed, no candidate produced, `remaining` empty. That plan is handed back so
 *  the director finishes it - it is never a list of items to dispatch again. */
export function unfinishedWithoutChild(plans, liveSessions, settled = new Set()) {
  return plans.filter(p => !liveSessions.has(p.child_session) && !settled.has(p.attempt_id)
      && (p.remaining?.length || awaitingFinish(p)))
    .map(p => ({ attempt_id: p.attempt_id, node_key: p.node_key, next: p.next, remaining: p.remaining ?? [],
      ready_to_finish: awaitingFinish(p) }));
}

/** Whether the director may take a plan over from a named session, or why not.
 *
 *  A host restart can kill a native child mid-plan: the plan stays bound to a
 *  session that no longer exists, so `todo_done` answers `bound_to_other_session`
 *  forever and `todo_finish` refuses (`never begun`), which in turn keeps
 *  `idle_dormant` refusing `unfinished_plans` for as long as the attempt itself is
 *  live. The exit is one deliberate takeover, and its whole safety is the liveness
 *  check: only the session the plan is actually bound to may be replaced, and only
 *  while it is provably not running. A live child keeps its plan and keeps
 *  answering `bound_to_other_session`.
 */
export function rebindRefusal({ attempt_id, item_id, session_id, bound_session, live = [], root_session }) {
  if (!attempt_id || !item_id) return { refused: 'invalid_arguments', reason: 'attempt_id and the item_id to take over are required' };
  if (!session_id) return { refused: 'invalid_arguments', reason: 'session_id must name the session being replaced' };
  if (session_id === root_session || live.includes(session_id))
    return { refused: 'live_session', reason: `session ${session_id} is still alive, so it still owns its plan` };
  if (bound_session !== session_id)
    return { refused: 'not_bound_session', reason: `plan ${attempt_id} is bound to ${bound_session ?? 'no session'}, not ${session_id}` };
  return null;
}

/** Grep the immutable raw log, including events shadowed by compaction. */
export function grepHistory(events, pattern, { regex = false, ignore_case = false, after_seq = -1, limit = 20 } = {}) {
  if (!pattern) throw new Error('pattern must not be empty');
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('limit must be 1–100');
  const matcher = regex ? new RegExp(pattern, ignore_case ? 'i' : '') : null;
  const needle = ignore_case ? pattern.toLocaleLowerCase() : pattern;
  const matches = [];
  let more = false, next = after_seq;
  for (const event of events) {
    if (event.seq <= after_seq || !['user/message', 'assistant/message', 'tool/call', 'tool/result'].includes(event.type)) continue;
    const body = JSON.stringify(event.data, null, 2);
    const found = body.split('\n').flatMap((text, index) => {
      const hit = matcher ? (matcher.exec(text)?.index ?? -1) : (ignore_case ? text.toLocaleLowerCase() : text).indexOf(needle);
      const start = Math.max(0, hit - 200);
      return hit >= 0 ? [{ line: index + 1, column: hit + 1, text: text.slice(start, start + 1200), truncated: text.length > 1200 }] : [];
    });
    if (!found.length) continue;
    if (matches.length === limit) { more = true; break; }
    matches.push({ seq: event.seq, type: event.type, matches: found.slice(0, 10), omitted_lines: Math.max(0, found.length - 10) });
    next = event.seq;
  }
  return { matches, next_after_seq: next, has_more: more };
}

// Pure transport/state controller is separate so the actual wait/refresh logic
// is exercised without making model calls.
export class DriverRuntime {
  constructor({ project, driver, directory, request, onEvent = () => {}, onConnectionError = () => {}, onConnectionRestored = () => {}, retryDelayMs = 1000, todoPlans = async () => [] }) {
    Object.assign(this, { project, driver, directory, request, onEvent, onConnectionError, onConnectionRestored, retryDelayMs, todoPlans });
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
    try { this.todo = await this.todoPlans(); this.todoError = undefined; }
    catch (error) { this.todo = []; this.todoError = String(error.message ?? error).slice(0, 1000); }
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

/**
 * The tool names a resumed session's recorded request envelope still advertises
 * but this process does not offer.
 *
 * `request/header` records the envelope of the LAST request DSH routed for that
 * session. A restart may boot into a different execution mode or a different
 * runtime build, so the recorded envelope can name a tool this process
 * deliberately never registers - on the host, the workspace-write coding bridge
 * (`project_work`). Nothing here can intervene at the call that consumes it:
 * DSH's compaction backend replays the recorded envelope VERBATIM as the
 * cacheable prefix of its summarization request (`buildSummarizationInput` in
 * @deepseek-ai/dsh-compaction-basic, `...header?.tools`), and that request is
 * the first model call of a resumed session - it runs from `agent/pre-step`
 * before the loop ever writes a fresh envelope. So the runtime has to retire
 * the tool from the RECORD, not from the registry: see
 * retireStaleToolEnvelope.
 * @param recorded - the folded `request/header` of the session, if any.
 * @param offered - the tool schemas this process offers the agent.
 * @returns the retired names, deduplicated, in recorded order.
 */
export function staleRecordedTools(recorded, offered = []) {
  const tools = recorded?.tools;
  if (!Array.isArray(tools) || tools.length === 0) return [];
  const names = new Set(offered.map(tool => tool?.name));
  return [...new Set(tools.map(tool => tool?.name).filter(name => !names.has(name)))];
}

/**
 * Retire those tools from the session's recorded request envelope.
 *
 * Everything else is kept byte for byte - same config, same adapter defaults,
 * same schemas in the same order - because the point of the recorded envelope
 * is a genuine prefix of the conversation for provider cache reuse, and only
 * the retired entries are a claim about capabilities this process does not
 * have. The removed schema breaks that prefix either way; leaving it in ALSO
 * asks the provider to expose a tool the driver cannot call.
 *
 * Appends one `request/header` with reason `change`, which is exactly what the
 * loop appends for a tool change; the fold keeps the last one in force.
 * @param session - the resumed DSH session.
 * @param offered - the tool schemas this process offers the agent.
 * @returns the retired names; empty when the envelope already agrees.
 */
export function retireStaleToolEnvelope(session, offered = []) {
  const recorded = session?.requestHeader?.();
  const stale = staleRecordedTools(recorded, offered);
  if (stale.length === 0) return [];
  const retired = new Set(stale);
  const header = { ...recorded, tools: recorded.tools.filter(tool => !retired.has(tool?.name)) };
  // An empty tool list is not a header field: DSH refuses the event.
  if (header.tools.length === 0) delete header.tools;
  session.append('request/header', { header, reason: 'change' });
  return stale;
}

export async function boot(ctx, { env = process.env, request: suppliedRequest } = {}) {
  // see dropIndexedGitConfig: the tool shell's secret-name filter leaves COUNT without KEY_0
  dropIndexedGitConfig(process.env);
  await ctx.get('loader')?.await();
  const require = createRequire(env.AITELIER_DSH_ENTRY);
  const { defineTool } = await import(require.resolve('@deepseek-ai/dsh-tools'));
  const llm = await import(require.resolve('@deepseek-ai/dsh-llm'));
  const { createUserMessage } = llm;
  installToolDiscovery(ctx, { defineTool, HarnessError: llm.HarnessError, rootSession: env.AITELIER_DSH_SESSION });
  const { installModelSelection } = await import(require.resolve('@deepseek-ai/dsh-agent'));
  const token = (await readFile(env.AITELIER_DRIVER_TOKEN_FILE, 'utf8')).trim();
  const request = suppliedRequest ?? (async (path, body, signal) => {
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
  // `?? ''` keeps this read on the runtime's OWN env mapping: an unset key must
  // not fall through to the helper's process.env default (which resolves to the
  // 180 s budget anyway).
  installHostModel(ctx, llm, { request, project: env.AITELIER_DSH_PROJECT, session: env.AITELIER_DSH_SESSION,
    rootEffort: rootReasoningEffort(env.AITELIER_DSH_ROOT_REASONING_EFFORT),
    retry: { budgetMs: modelRetryBudgetMs(env.AITELIER_DSH_MODEL_RETRY_SEC ?? '') } });
  // Resolve the existing mounted secret file into DSH's private credential
  // store, never into the environment inherited by test/build subprocesses.
  const secretDir = env.AITELIER_SECRETS_DIR ?? '/run/aitelier-secrets';
  try {
    const key = env.AITELIER_DSH_PROVIDER === 'deepseek-official' ? (await readFile(join(secretDir, 'DEEPSEEK_API_KEY'), 'utf8')).trim() : '';
    if (key) await ctx.credentials.set('DEEPSEEK_API_KEY', key);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const project = env.AITELIER_DSH_PROJECT;
  const hostExecution = env.AITELIER_DSH_EXECUTION === 'host';
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
  const todo = new TodoCli({ python: env.AITELIER_TODO_PYTHON, cwd: env.AITELIER_TODO_CWD, stateDir: env.AITELIER_DSH_STATE_DIR });
  // The live native children, and what it takes for the runtime to believe one is
  // gone: the agent registry answers with the agent, or undefined when no live agent
  // has that id. A missing registry proves nothing, so an id it cannot be asked about
  // stays. Every reader goes through this object, which reconciles on read - see
  // LiveChildren. `pruneChildren` keeps the ops log line for the ids a read dropped.
  const children = new LiveChildren({ isLive: id =>
    typeof ctx.agents?.get !== 'function' || ctx.agents.get(id) !== undefined });
  const pruneChildren = () => {
    const departed = children.reconcile();
    if (departed.length) ctx.logger.info(`dropped departed child sessions ${departed.join(', ')}: no live agent holds them`);
    return departed;
  };
  const jsonOutput = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
  const planRef = { attempt_id: { type: 'string', required: true }, item_id: { type: 'string', required: true } };
  // Worker-facing plan tools are host-scoped like project_work: the caller is
  // exec.agent, so a child's todo_begin binds the CHILD session to the plan.
  ctx.tools.register(defineTool({ name: 'todo_status',
    description: 'Read the frozen todo plan of a State attempt: brief, goal, next item, done and remaining items. Call it first, and again whenever you lost track (for example after compaction).',
    parameters: { attempt_id: { type: 'string', required: true } }, output: jsonOutput,
    execute: (args, exec) => todo.call(args.attempt_id, ['status', '--attempt=' + args.attempt_id], exec.signal),
  }));
  ctx.tools.register(defineTool({ name: 'todo_begin',
    description: 'Start one plan item before working on it. Refuses unmet needs, captures the state its predicates compare against, and binds the plan to your session.',
    parameters: planRef, output: jsonOutput,
    async execute(args, exec) {
      // The CLI refuses, under the plan lock, to take a plan bound to another
      // session. Rebind only when that session is verified dead: not the root
      // and not a live child of this process.
      const begin = ['begin', '--attempt=' + args.attempt_id, '--item=' + args.item_id, '--session=' + exec.agent.session.id];
      const result = await todo.call(args.attempt_id, begin, exec.signal);
      if (result.refused !== 'bound_to_other_session') return result;
      const bound = result.bound_session;
      // A plan stays bound only to a session that is still a live child: `has` reads
      // the reconciled set, so a child that ended without its end edge no longer makes
      // a stranded plan look attended.
      if (!bound || bound === env.AITELIER_DSH_SESSION || children.has(bound)) return result;
      return todo.call(args.attempt_id, [...begin, '--rebind'], exec.signal);
    },
  }));
  ctx.tools.register(defineTool({ name: 'todo_done',
    description: 'Say you finished one plan item. The harness evaluates its bound predicates now and returns outcome pass|fail|absent with each result; your own claim is not an input. A fail keeps the item open: fix it and call again.',
    parameters: planRef, output: jsonOutput,
    execute: async (args, exec) => todo.call(args.attempt_id, ['done', '--attempt=' + args.attempt_id, '--item=' + args.item_id,
      '--session=' + exec.agent.session.id], exec.signal,
      await todo.bound(args.attempt_id, status => status.timeout_budget_seconds?.[args.item_id], exec.signal)),
  }));
  ctx.tools.register(defineTool({ name: 'todo_report',
    description: 'Attach one free-text note to a plan item. It changes no item state.',
    parameters: { ...planRef, note: { type: 'string', required: true } }, output: jsonOutput,
    execute: (args, exec) => todo.call(args.attempt_id, ['report', '--attempt=' + args.attempt_id, '--item=' + args.item_id,
      '--session=' + exec.agent.session.id, '--note=' + args.note], exec.signal),
  }));
  // The plans, and what State says about the attempt each one is named after. The
  // two are read together because every view of "unfinished work" needs both: a
  // plan whose attempt has settled is not work, and one whose attempt State could
  // not answer for still is (settledAttempts fails closed).
  const planWork = async signal => {
    const plans = await todo.plans(signal);
    return { plans, settled: await settledAttempts(plans, request, signal) };
  };
  const runtime = new DriverRuntime({ project, driver: env.AITELIER_DRIVER_ID,
    directory: env.AITELIER_DSH_STATE_DIR, request,
    todoPlans: async () => { const { plans, settled } = await planWork();
      return todoSummary(plans, children.sessionSet(), settled); } });
  await runtime.init();
  const schema = await request('/api/state/schema');
  const watchIntervalMs = Number(env.AITELIER_DSH_WATCH_INTERVAL_MS) > 0 ? Number(env.AITELIER_DSH_WATCH_INTERVAL_MS) : 15000;
  const floorEnv = Number(env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS);
  const compactFloor = env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS !== undefined && Number.isFinite(floorEnv) && floorEnv >= 0
    ? floorEnv : DORMANT_COMPACT_MIN_TOKENS;
  // idleReview is created after the agent; tools reach it only when called.
  let agent, agentCtx, idleReview, waiting = false, disposed = false;
  // watch_process: pid -> { note, since }. One poller while any watch exists.
  const watches = new Map();
  // What the director is waiting on that will wake it: its live children and watched
  // jobs. `continuationWaitKey` reconciles the children, so a departed one leaves the
  // key - a synchronous read of a synchronous oracle, no await needed or available here.
  const continuation = new Continuation({ waitKey: () => continuationWaitKey({ children,
    watched: [...watches.keys()].sort((a, b) => a - b) }) });
  let watchTimer;
  const pollWatches = () => {
    for (const [pid, watch] of watches) {
      if (processAlive(pid) && processSession(pid) === env.AITELIER_DSH_SESSION) continue;
      watches.delete(pid);
      void notify('AItelier background job finished:\n' + JSON.stringify({ pid, note: watch.note,
        ran_seconds: Math.round((Date.now() - watch.since) / 1000) })).catch(error => ctx.logger.warn(String(error)));
    }
    if (!watches.size) { clearInterval(watchTimer); watchTimer = undefined; }
  };
  let subscriptionError = '', completionError = '';
  let activity = Promise.resolve();
  const currentPhase = () => waiting ? completionError ? 'failed' : 'waiting' : 'running';
  const report = (phase, error = completionError || subscriptionError, event) => {
    // One report per /activity request; `phaseReport` names the live child SESSIONS,
    // reconciled against the registry, never the runIds the registry cannot answer for.
    const body = phaseReport({ phase, children, error, event });
    activity = activity.catch(() => {}).then(() => request(`/api/dsh-drivers/${encodeURIComponent(project)}/activity`,
      body));
    activity.catch(error => ctx.logger.warn(String(error)));
    return activity;
  };
  // Two distinct producers, two distinct kinds. A State event is NOT the user
  // speaking: labelling both `user` made a driver unable to tell "State changed"
  // from "the owner wrote to me", which is exactly how a routine tick gets
  // mistaken for an unanswered message. DSH's MessageSourceMap is merge-extensible
  // (each producer declares its own kind, consumers fall through unknown ones).
  const eventMessage = text => createUserMessage({ content: [{ type: 'text', text }], source: { kind: runtimeEventSource } });
  const notify = async text => {
    if (disposed) return;
    ctx.agents.withoutInitiator(() => agent.steer(eventMessage(text)));
    await ctx.sessionPersistence.flush();
  };
  // Bounded and self-describing: the newest page of the matches, projected (see
  // listProjectInbox / projectInboxDigest). An injection is re-sent on every
  // director-message wake, so it must not grow with project history - and it must
  // be the page a message that just arrived is on, not the oldest one.
  const projectInbox = async () => projectInboxDigest(await listProjectInbox(request, project));
  runtime.onConnectionError = async error => {
    subscriptionError = connectionErrorMessage(error);
    ctx.logger.warn(subscriptionError);
    await report(currentPhase()).catch(() => {});
  };
  runtime.onConnectionRestored = () => { subscriptionError = ''; return report(currentPhase()); };
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
    await notify('AItelier runtime event:\n' + JSON.stringify({ ...result, project_inbox: inbox }));
  };
  const setup = async (scope, subject) => {
    agentCtx = scope;
    scope.systemPrompt.section({ name: 'aitelier:driver-goal', order: 10, interpolate: false,
      text: () => driverGoalSection(ctx.goals, subject) });
    scope.systemPrompt.section({ name: 'aitelier:driver-protocol', order: 11, interpolate: false,
      text: () => runtime.snapshot.driver_skill ?? '' });
    scope.systemPrompt.section({ name: 'aitelier:driver-runtime', order: 12, interpolate: false,
      text: runtimeSkill });
    installModelSelection(scope, { current: { provider: env.AITELIER_DSH_PROVIDER,
      model: env.AITELIER_DSH_MODEL }, assembled: undefined });
    const injection = new StateInjection(subject.session, runtime, createUserMessage);
    scope.on('session/event', (session, event) => {
      if (session === subject.session) injection.observe(event);
    });
    scope.on('agent/pre-step', async (_payload, next) => {
      const decision = await next(); // Includes the base automatic compactor.
      if (decision.kind !== 'enter') return decision;
      const message = await injection.prepare();
      return message ? { ...decision, messages: [...decision.messages, message] } : decision;
    });
    scope.on('agent/request', async (_payload, next) => {
      const config = await next();
      await injection.beforeRequest();
      return config;
    });
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
    // Director-only plan tools: scope-registered, so native children never see them.
    // A plan is expanded from a reviewed repository template, never authored per
    // dispatch: there is deliberately no way to pass items through this tool.
    scope.tools.register(defineTool({ name: 'todo_templates',
      description: 'Director only. List the repository todo templates (name, version, description, params with type/required/default) a plan can be expanded from.',
      parameters: {}, output: jsonOutput,
      async execute(_args, exec) {
        const { code, result } = await todo.run(['templates'], exec.signal);
        return code === 2 ? { refused: true, ...result } : result;
      },
    }));
    const planArgs = new Set(['template', 'params', 'goal', 'attempt_id', 'node_key', 'context_hash', 'base_sha', 'workspace']);
    scope.tools.register(defineTool({ name: 'todo_plan',
      description: 'Director only. Freeze the todo plan of a State attempt before dispatching its native child, by expanding a repository template (see todo_templates) with params bound to that template\'s declared params. context_hash is the one start_external_attempt returned. A wrongly typed, missing or unknown param refuses the plan. Items cannot be supplied: when no template fits, add one under todo_templates/ through a reviewed code change.',
      parameters: { template: { type: 'string', required: true }, params: { type: 'object', additionalProperties: true, required: true },
        goal: { type: 'string', required: true }, attempt_id: { type: 'string', required: true }, node_key: { type: 'string', required: true },
        context_hash: { type: 'string', required: true }, base_sha: { type: 'string', required: true }, workspace: { type: 'string', required: true } },
      output: jsonOutput,
      execute: (args, exec) => Object.keys(args).some(key => !planArgs.has(key))
        ? { refused: 'unknown_arguments', reason: `todo_plan takes only ${[...planArgs].join(', ')}; items come from the template`, brief: null }
        : todo.call(args.attempt_id, ['create', '--template=' + args.template, '--params=' + JSON.stringify(args.params),
        '--attempt=' + args.attempt_id, '--node-key=' + args.node_key, '--context-hash=' + args.context_hash,
        '--base-sha=' + args.base_sha, '--workspace=' + args.workspace, '--goal=' + args.goal], exec.signal),
    }));
    scope.tools.register(defineTool({ name: 'todo_finish',
      description: 'Director only. Re-evaluate every item of an attempt\'s plan; only when all pass, write the git bundle, its sha256 and the candidate report and return their paths for report_external_attempt. Otherwise it is refused with the failing items.',
      parameters: { attempt_id: { type: 'string', required: true } }, output: jsonOutput,
      execute: async (args, exec) => /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(args.attempt_id)
        ? todo.call(args.attempt_id, ['finish', '--attempt=' + args.attempt_id,
          '--out=' + todo.finishDir(args.attempt_id)], exec.signal,
          await todo.bound(args.attempt_id, status => status.finish_budget_seconds, exec.signal))
        : { refused: 'invalid_attempt_id', reason: 'attempt_id must be a plain file name', brief: null },
    }));
    // The escape hatch for a plan whose child died mid-item (a host restart
    // kills it): the director names the dead session and takes the item over.
    // Without this an item already begun by a dead session has no exit at all -
    // todo_done answers bound_to_other_session, todo_finish refuses, and
    // idle_dormant refuses unfinished_plans for as long as the attempt is live.
    // (A plan whose attempt has already settled does not block at all; the takeover
    // is for the stranded plan of an attempt that is still open.)
    scope.tools.register(defineTool({ name: 'todo_rebind',
      description: 'Director only. Take a plan item over from a session that died (for example a native child killed by a host restart) so its work can be finished. Names the attempt_id, the item_id to begin and the session_id being replaced; refused unless that session is the one the plan is bound to and is verifiably dead. The plan records the takeover (rebound_from), so a later reader sees it changed hands. A live child keeps its own plan.',
      parameters: { attempt_id: { type: 'string', required: true }, item_id: { type: 'string', required: true },
        session_id: { type: 'string', required: true } },
      output: jsonOutput,
      async execute(args, exec) {
        const root = exec.agent.session.id;
        let status;
        try { status = await todo.call(args.attempt_id, ['status', '--attempt=' + args.attempt_id], exec.signal); }
        catch (error) { return { refused: 'unverifiable', reason: 'could not read the plan: ' + String(error.message ?? error).slice(0, 500), brief: null }; }
        if (status.refused) return status;
        const refusal = rebindRefusal({ attempt_id: args.attempt_id, item_id: args.item_id, session_id: args.session_id,
          // Reconciled: a child that ended without its end edge is provably dead, so
          // its stranded plan may be taken over.
          bound_session: status.child_session, live: children.ids(), root_session: root });
        if (refusal) return { ...refusal, brief: status.brief ?? null };
        // The CLI holds the plan lock and writes the file; --rebind is the one path
        // that may move child_session, and it records the session it replaced.
        const result = await todo.call(args.attempt_id, ['begin', '--attempt=' + args.attempt_id, '--item=' + args.item_id,
          '--session=' + root, '--rebind'], exec.signal);
        return { ...result, replaced_session: args.session_id, taken_over_by: root };
      },
    }));
    scope.tools.register(defineTool({ name: 'watch_process',
      description: 'Director only. Watch a long background job you started (its pid): when it exits you get a runtime event with the pid, your note and how long it ran, even while dormant. Use it instead of polling a log; a watched job does not block idle_dormant. Refused for a pid that is not running or not started from this driver.',
      parameters: { pid: { type: 'integer', required: true }, note: { type: 'string', required: true } }, output: jsonOutput,
      execute(args) {
        const pid = Number(args.pid), note = typeof args.note === 'string' ? args.note.trim().slice(0, 500) : '';
        const refusal = watchRefusal({ pid, note, alive: Number.isSafeInteger(pid) && pid > 1 && processAlive(pid),
          session: processSession(pid), mySession: env.AITELIER_DSH_SESSION, watched: watches.has(pid), count: watches.size });
        if (refusal) return refusal.refused === 'too_many_watches' ? { ...refusal, watching: [...watches.keys()] } : refusal;
        watches.set(pid, { note, since: watches.get(pid)?.since ?? Date.now() });
        if (!watchTimer) { watchTimer = setInterval(pollWatches, watchIntervalMs); watchTimer.unref?.(); }
        return { watching: true, pid, note, check_every_seconds: watchIntervalMs / 1000 };
      },
    }));
    scope.tools.register(defineTool({ name: 'idle_dormant',
      description: 'Director only. Call when no authorized work is available and nothing is in flight: stops the 15-minute idle review (no model turns while nothing happens) and compacts a large context once. You stay subscribed: any State event, inbox message, owner message or child message wakes you and the review resumes. Refused while you have a live child, a live State claim or an unfinished todo plan whose State attempt has not settled.',
      parameters: { reason: { type: 'string', required: true } }, output: jsonOutput,
      async execute(args, exec) {
        const reason = typeof args.reason === 'string' ? args.reason.trim() : '';
        // Reconcile first: a departed child must not refuse dormancy at all (and the
        // readers below reconcile on their own, so an early return cannot skip it).
        pruneChildren();
        const live = children.ids();
        const early = dormancyRefusal({ reason, liveChildren: live.length, childSessions: live });
        if (early) return early;
        let claims, work;
        try {
          const bounded = AbortSignal.any([exec.signal, AbortSignal.timeout(30000)]);
          const listed = await request('/api/state/query/list_claims', { project_id: project, driver_id: env.AITELIER_DRIVER_ID, limit: 100 },
            bounded);
          claims = ((listed.result ?? listed).claims ?? []).filter(c => c.status === 'live' && c.driver_id === env.AITELIER_DRIVER_ID);
          work = await planWork(bounded);
        } catch (error) {
          return { refused: 'unverifiable', reason: 'could not prove nothing is in flight: ' + String(error.message ?? error).slice(0, 500) };
        }
        // The reads above take time: re-reconcile before reading the plans and refusing.
        pruneChildren();
        const liveAfter = children.ids();
        const plans = unfinishedWithoutChild(work.plans, new Set(liveAfter), work.settled);
        const background = backgroundProcesses(env.AITELIER_DSH_SESSION).filter(pid => !watches.has(pid));
        const refusal = dormancyRefusal({ reason, liveChildren: liveAfter.length, childSessions: liveAfter,
          claims, plans, background });
        if (refusal) return refusal;
        if (!idleReview) return { refused: 'starting', reason: 'the runtime is still starting' };
        const dormant = idleReview.enterDormancy(reason);
        return { dormant: true, since: dormant.since, reason,
          compaction: `once on the next idle if the context is at least ${compactFloor} tokens`,
          wakes_on: ['State event', 'inbox message', 'owner message', 'child message'] };
      },
    }));
    scope.tools.register(defineTool({ name: 'grep_chat_history',
      description: 'Search this driver’s complete chat log, including before compaction. Literal search by default; optional JavaScript regex. Results cite event seq and line. Page with after_seq.',
      parameters: { pattern: { type: 'string', required: true }, regex: { type: 'boolean' },
        ignore_case: { type: 'boolean' }, after_seq: { type: 'integer' }, limit: { type: 'integer' } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(args, exec) {
        const observation = await ctx.sessionQuery.observeSession(subject.session.id, { signal: exec.signal, projectionMode: 'none' });
        try { return { session_id: subject.session.id, ...grepHistory(observation.events, args.pattern, args) }; }
        finally { observation[Symbol.dispose](); }
      },
    }));
  };
  const options = { agentOptions: { provider: env.AITELIER_DSH_PROVIDER, model: env.AITELIER_DSH_MODEL }, setup };
  const resume = await ctx.sessionPersistence.stat(env.AITELIER_DSH_SESSION) !== undefined;
  const handle = resume ? await ctx.agents.resume({ ...options, resumeSessionId: env.AITELIER_DSH_SESSION })
    : await ctx.agents.create({ ...options, sessionId: env.AITELIER_DSH_SESSION, meta: { cwd: process.cwd() } });
  agent = handle.agent;
  // A restart boots with whatever the previous process recorded, and the first
  // model call of the resumed session replays that record verbatim (see
  // retireStaleToolEnvelope). Before the agent can take a step, make the record
  // agree with what this process actually offers: on the host that retires the
  // workspace-write coding bridge, so a resumed host session never asks the
  // provider for a tool this runtime does not register.
  try {
    // The predicate that decides the registration is `hostExecution` (lines 861
    // and 864): `env.AITELIER_DSH_EXECUTION === 'host'`, and `project_work` is
    // registered only when `!hostExecution`. The host prompt (line 881) declares
    // that bridge not mounted. A persisted envelope may predate that predicate,
    // so this retirement compares recorded names with the offered registry rather
    // than re-testing the boolean.
    const retired = retireStaleToolEnvelope(agent.session, ctx.tools.schemas(agent));
    if (retired.length > 0) ctx.logger.info(`recorded request envelope retired ${retired.join(', ')}: not offered by this runtime`);
  } catch (error) {
    // Never take the driver down over a repair: the next routed request writes
    // a fresh envelope anyway, which is what makes the stale record vanish.
    ctx.logger.warn('recorded request envelope not retired: ' + String(error).slice(0, 300));
  }
  idleReview = new IdleReview({ agent, goals: ctx.goals, continuation,
    canContinue: () => !completionError,
    ask: async () => {
      if (disposed) return;
      ctx.agents.withoutInitiator(() => agent.steer(createUserMessage({ content: [{ type: 'text', text: continuationQuestion }], source: { kind: continueSource } })));
      await ctx.sessionPersistence.flush();
    },
    ...(Number(env.AITELIER_DSH_IDLE_REVIEW_MS) > 0 ? { intervalMs: Number(env.AITELIER_DSH_IDLE_REVIEW_MS) } : {}),
    compact: async () => {
      const tokens = ctx.tokenMeter.measure(agent.session).totalTokens;
      if (!(tokens >= compactFloor)) return ctx.logger.info(`dormant at ${tokens} tokens: below ${compactFloor}, not compacting`);
      // Native idle compaction; refused (busy) when work arrived meanwhile - then the wake handles it.
      const result = await ctx.compaction.compactNow(agent, AbortSignal.timeout(10 * 60 * 1000));
      ctx.logger.info(`dormant at ${tokens} tokens: ${result ? 'compacted' : 'nothing safe to compact'}`);
    },
    notify: async () => {
      let todoView;
      try {
        const { plans, settled } = await planWork();
        todoView = { todo_unfinished: unfinishedWithoutChild(plans, children.sessionSet(), settled) };
      }
      catch (error) { ctx.logger.warn(String(error)); todoView = { todo_error: String(error.message ?? error).slice(0, 1000) }; }
      await notify('AItelier director review due:\n' + JSON.stringify({
        project_id: project, reason: 'idle_review_due',
        instruction: reviewInstruction,
        ...todoView,
      }));
    },
    onError: error => { ctx.logger.warn(String(error)); },
  });
  ctx.on('agent/status', ({ agent: subject, status }) => {
    if (subject !== agent || disposed) return;
    idleReview.statusChanged(status);
    waiting = status === 'idle';
    void report(currentPhase());
  });
  if (hostExecution) {
    // Resume preserves old pinned policy. Change it through DSH's durable API;
    // children inherit this explicit full-access selection, including forks.
    ctx.get('permissionPresets').set(agent.session, 'danger-full-access');
  }
  ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return;
    // Only real input ends dormancy (a compaction while dormant must not). A kind
    // missing from wakeSources only keeps the timed review parked; the message
    // itself still runs a turn.
    continuation.observe(event);
    if (endsDormancy(event) && idleReview.wake())
      ctx.logger.info(`dormancy ended by ${event.data.source?.kind ?? 'user'} message at seq ${event.seq}`);
    if (event.type === 'assistant/message') completionError = '';
    const entry = historyPage([event], { limit: 1 }).entries[0];
    if (entry) void report(currentPhase(), undefined, entry);
  });
  // A child that stops with plan items open is handed back by the runtime, not
  // left to whatever its last message happened to say.
  // Keyed to the plan's bound session only, so a child's own helpers ending
  // does not hand its plan back.
  const handback = async session => {
    let plans;
    try { plans = await todo.plans(); }
    catch (error) {
      // The director must still learn that a child ended while plans were unreadable.
      return notify('AItelier todo handback:\n' + JSON.stringify({ reason: 'todo_handback_failed', child_session: session,
        todo_error: String(error.message ?? error).slice(0, 1000) }));
    }
    for (const plan of plans) {
      // Items open only: a plan whose items have all passed is not work to run
      // again. It reaches the director as ready_to_finish through the plan and
      // work views (awaitingFinish), which is where that reminder belongs.
      if (plan.child_session !== session || !plan.remaining?.length) continue;
      let payload;
      try {
        const { code, result } = await todo.run(['handback', '--attempt=' + plan.attempt_id]);
        payload = code === 0 ? { done: result.done, next: result.next, remaining: result.remaining, last_observations: result.last_observations }
          : { todo_error: JSON.stringify(result).slice(0, 1000) };
      } catch (error) { payload = { todo_error: String(error.message ?? error).slice(0, 1000) }; }
      await notify('AItelier todo handback:\n' + JSON.stringify({ reason: 'todo_unfinished', attempt_id: plan.attempt_id, ...payload }));
    }
  };
  // Observe nested native children too: all activity belongs to this private
  // per-DAG process, so no cross-project worker can enter these counts.
  ctx.on('subagent/start', info => {
    children.start(info);
    void report(currentPhase());
  });
  ctx.on('subagent/end', info => {
    children.end(info); void report(currentPhase());
    void handback(info.id).catch(error => ctx.logger.warn(String(error)));
  });
  // After a child's successful compaction, its next step gets one line of its
  // bound plan's brief. Host-scoped hooks see native children; the root has
  // its own post-compaction State snapshot instead.
  const compactedChildren = new Set();
  ctx.on('session/event', (session, event) => {
    if (event.type === 'compaction/end' && !event.data.error && session.id !== env.AITELIER_DSH_SESSION) compactedChildren.add(session.id);
  });
  ctx.on('agent/pre-step', async (payload, next) => {
    const subject = payload.agent;
    const decision = await next();
    if (decision.kind !== 'enter' || !compactedChildren.delete(subject.session.id)) return decision;
    let text;
    try {
      const plan = (await todo.plans(payload.signal)).find(p => p.child_session === subject.session.id);
      if (!plan) return decision;
      ({ brief: text } = await todo.call(plan.attempt_id, ['status', '--attempt=' + plan.attempt_id], payload.signal));
    } catch (error) { ctx.logger.warn('todo brief skipped: ' + String(error)); return decision; }
    if (typeof text !== 'string') return decision;
    const brief = createUserMessage({ content: [{ type: 'text', text: 'todo brief: ' + String(text).replace(/\s*\n\s*/g, ' / ') }],
      source: { kind: todoBriefSource } });
    return { ...decision, messages: [...decision.messages, brief] };
  });
  ctx.on('agent/error', ({ agent: subject, error }) => {
    if (subject !== agent) return;
    completionError = connectionErrorMessage(error, 'AItelier model completion');
    void report('failed');
  });
  const close = async () => {
    if (disposed) return;
    disposed = true;
    idleReview.close();
    clearInterval(watchTimer);
    // A host shutdown ends process-local continuation, not the standing goal.
    // Disarm before cancellation so DSH does not persist a user-style pause.
    ctx.goals.disarm(agent);
    runtime.close();
    await handle.dispose();
    await ctx.sessionPersistence.flush();
    await activity.catch(() => {});
  };
  ctx.on('dispose', close);
  await report('running');
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
    queueMicrotask(() => { if (!disposed) ctx.agents.withoutInitiator(deliverInbox); });
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
  const pump = runtime.pump().catch(async error => {
    if (!runtime.controller.signal.aborted) {
      await report('failed', String(error).slice(0, 2000));
      throw error;
    }
  });
  pump.catch(error => { ctx.logger.error(String(error)); ctx.get('appExit')?.(1); });
  return { runtime, agent, agentCtx, close, pump };
}

export function apply(ctx) {
  // Awaiting loader settlement inside apply would deadlock its own startup.
  void boot(ctx).catch(error => { console.error('AItelier DSH startup:', String(error)); ctx.get('appExit')?.(1); });
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
