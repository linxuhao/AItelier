/** dsh-director's runtime pieces that need no backend: continuation, dormancy,
 *  live children, process watches, todo plan views and history grep.
 *
 *  Moved verbatim from integrations/dsh-driver/runtime.mjs (plugin design step 2,
 *  "pure move"). The only changes are the parameters below, each of which used to
 *  be a backend constant baked into the code and is now supplied by the host
 *  (the backend adapter passes exactly the old values, so its behaviour and its
 *  request bodies do not change):
 *    - message source kinds and section names (`labels`);
 *    - texts that name the host's tools (`texts`);
 *    - the environment variable that tags this driver's processes (`sessionEnv`);
 *    - the todo CLI's module and state-directory variable (TodoCli options);
 *    - how a work item's settlement is read (`settledAttempts(plans, workSettled)`).
 *  Nothing here may import the adapter or name its backend. */
import { readFileSync, readdirSync } from 'node:fs';
import { stat } from 'node:fs/promises';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

/** Message source kinds and prompt-section names a host may rename. They are
 *  persisted in session logs and carried in request bodies, so a host that already
 *  has sessions keeps its own values. */
export const defaultLabels = Object.freeze({
  runtimeEvent: 'dsh-director-runtime-event',
  continue: 'dsh-director-continue',
  todoBrief: 'dsh-director-todo-brief',
  context: 'dsh-director-context',
  contextSection: 'dsh-director:context',
  sectionPrefix: 'dsh-director',
});

/** The marker a director answers with when nothing is actionable. */
export const NOTHING_LEFT = 'NOTHING_LEFT';
/** Continuation questions in a row without real input before the director is left idle. */
export const MAX_AUTO_CONTINUE = 5;

/** Texts the runtime sends or registers that a host may reword (they name its own
 *  tools or vocabulary). A host overrides any subset; the rest stay these. */
export function defaultTexts() {
  const askTheUser = 'Anything that waits on your user (a ruling, an authorization, a hold only they can lift) must be an open question they can see: ask each such decision that is not already open before you answer NOTHING_LEFT or go dormant.';
  return {
    askTheUser,
    continuationQuestion: 'Continuation check: you ended a turn. Before parking, look for more work you can do now, in parallel with anything running: ready work items, next steps you promised yourself, results you can collect. If there is one, do it now. If not, answer with one line that starts with NOTHING_LEFT: naming why each candidate is blocked. For a long background job you started, call watch_process with its pid instead of polling it. ' + askTheUser,
    reviewInstruction: 'Recheck the ready work, next steps you promised yourself, results you can collect, worker capacity and prior blockers. Continue independent authorized work; a blocked task does not block the whole project. ' + askTheUser + ' If nothing is actionable, reply with one line that starts with NOTHING_LEFT: naming why each candidate is blocked; if nothing is in flight either, call idle_dormant with that reason and how long to sleep instead.',
    reviewDue: 'Director review due:\n',
    jobFinished: 'Background job finished:\n',
    todoHandback: 'Todo handback:\n',
    todoBatch: 'Todo batch:\n',
    modelCompletion: 'Model completion',
    slotFree: 'Worker slot free:\n',
    tools: {},
  };
}

/** One durable snapshot per session bootstrap / successful compaction epoch.
 *  `runtime` is the context source: `refresh()` then `render()` give the text after
 *  `header`. */
export class ContextInjection {
  constructor(session, runtime, createMessage, { kind = defaultLabels.context, section = defaultLabels.contextSection,
    header = 'Director context — startup / post-compaction snapshot:\n' } = {}) {
    Object.assign(this, { session, runtime, createMessage, kind, section, header });
    this.epoch = 0;
    this.injected = undefined;
    this.queued = undefined;
    for (const event of session.snapshotEvents()) this.observe(event);
  }
  observe(event) {
    if (event.type === 'compaction/end' && !event.data.error) this.epoch = event.seq;
    if (event.type === 'user/message' && event.data.source?.kind === this.kind) {
      this.injected = event.data.source.compaction_seq;
      this.queued = undefined;
    }
  }
  async prepare() {
    if (this.injected === this.epoch) return;
    await this.runtime.refresh();
    const text = this.header + this.runtime.render();
    const message = this.createMessage({ content: [{ type: 'text', text }], source: {
      kind: this.kind, form: 'snapshot', compaction_seq: this.epoch,
      sections: [{ name: this.section, text }],
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
export function connectionErrorMessage(error, source = 'Event subscription') {
  const cause = error.cause;
  return `${source}: ${error.message}${cause ? ` (${[cause.code, cause.message].filter(Boolean).join(': ')})` : ''}`.slice(0, 2000);
}

/** user/message sources that are input to the director: the owner, this runtime's
 *  host/inbox/handback events, and native child messages. */
export const wakeSourcesFor = (labels = defaultLabels) => new Set(['user', labels.runtimeEvent, 'agent-message', 'subagent-settled', 'child']);
/** Whether a root session event ends dormancy. DSH also appends its own user/messages
 *  (runtime context, compaction checkpoints, this runtime's context snapshot): not input. */
export const endsDormancyFor = (sources = wakeSourcesFor()) => event => event.type === 'user/message' && sources.has(event.data?.source?.kind ?? 'user');
const defaultEndsDormancy = endsDormancyFor();

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
  constructor({ maxAuto = MAX_AUTO_CONTINUE, waitKey = () => '', endsDormancy = defaultEndsDormancy } = {}) {
    this.maxAuto = maxAuto;
    this.waitKey = waitKey;
    this.endsDormancy = endsDormancy;
    this.auto = 0;
    this.tools = 0;
    this.text = '';
    this.answeredWait = undefined;
  }
  observe(event) {
    if (event.type === 'tool/call') this.tools++;
    else if (event.type === 'assistant/message') this.text = (event.data?.message?.content ?? [])
      .filter(block => block.type === 'text').map(block => block.text ?? '').join('\n');
    else if (this.endsDormancy(event)) this.auto = 0;
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

/** The environment variable a host sets on its driver process (and so on every job
 *  the driver's shell starts) to the driver's session id, when it names none. */
export const DEFAULT_SESSION_ENV = 'DSH_DIRECTOR_SESSION';
/** The driver session a process belongs to (its environment), or undefined. */
export function processSession(pid, procRoot = '/proc', sessionEnv = DEFAULT_SESSION_ENV) {
  try {
    const entry = readFileSync(join(procRoot, String(pid), 'environ'), 'latin1').split('\0').find(e => e.startsWith(sessionEnv + '='));
    return entry?.slice(sessionEnv.length + 1);
  } catch { return undefined; }
}
/** Whether a process exists and is not a zombie. */
export function processAlive(pid, procRoot = '/proc') {
  try { return readFileSync(join(procRoot, String(pid), 'stat'), 'latin1').replace(/^.*\) /s, '')[0] !== 'Z'; }
  catch { return false; }
}
/** Live processes other than the driver itself that carry this driver's session:
 *  jobs its shell started, detached or not (the environment survives reparenting). */
export function backgroundProcesses(session, selfPid = process.pid, procRoot = '/proc', sessionEnv = DEFAULT_SESSION_ENV) {
  if (!session) return [];
  let names;
  try { names = readdirSync(procRoot); } catch { return []; }
  return names.filter(name => /^\d+$/.test(name) && Number(name) !== selfPid)
    .map(Number).filter(pid => processSession(pid, procRoot, sessionEnv) === session && processAlive(pid, procRoot)).sort((a, b) => a - b);
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
 *  native child, work this driver holds (the host's heldWork), or an unfinished todo plan
 *  without a live child each refuse. claims/plans are omitted for the early check.
 *  `liveChildren` is the count the reason states; `childSessions` names them, so the
 *  refusal is actionable: a director can find the child and tell a live worker from
 *  an id whose lifecycle end edge was missed (iss-dc85aa2c80f2454d). A caller
 *  that knows only the count still refuses, it just cannot name the sessions.
 *  `isLive` rejects an id that is no longer a child at all; with sessions supplied
 *  the count is the number of ids that survive it, so a departed id neither
 *  refuses nor inflates the number. */
export function dormancyRefusal({ reason, liveChildren = 0, childSessions = [], claims = [], plans = [], background = [], isLive = () => true,
  heldReason = 'you hold work that is not finished' }) {
  if (!reason) return { refused: 'reason_required', reason: 'say why no work is available' };
  const given = childSessions ? [...childSessions] : [];
  const named = given.filter(isLive).sort();
  // Only a caller that cannot name its children falls back to its own count.
  const count = given.length ? named.length : liveChildren;
  if (count) {
    return { refused: 'live_children', reason: `${count} native child(ren) still running`,
      ...(named.length ? { live_children: named } : {}) };
  }
  if (claims.length) return { refused: 'live_claims', reason: heldReason, node_keys: claims.map(c => c.node_key) };
  if (plans.length) return { refused: 'unfinished_plans', reason: 'finish, report or hand back these plans first', attempts: plans.map(p => p.attempt_id),
    // Which of them have nothing left to run: the director's next step there is
    // todo_finish, not another dispatch.
    ...(plans.some(p => p.ready_to_finish) ? { ready_to_finish: plans.filter(p => p.ready_to_finish).map(p => p.attempt_id) } : {}) };
  if (background.length) return { refused: 'background_processes', reason: 'jobs you started are still running; watch_process them or wait', pids: background };
  return null;
}

/** How long a director may choose to stay dormant, in minutes: no shorter than
 *  the timed review it parks, no longer than a day. */
export const DORMANT_MIN_MINUTES = 15, DORMANT_MAX_MINUTES = 24 * 60;
/** Why a chosen sleep length must be refused, or null. */
export function wakeAfterRefusal(minutes) {
  if (Number.isInteger(minutes) && minutes >= DORMANT_MIN_MINUTES && minutes <= DORMANT_MAX_MINUTES) return null;
  return { refused: 'wake_after_minutes_invalid',
    reason: `say how long to stay dormant: an integer from ${DORMANT_MIN_MINUTES} to ${DORMANT_MAX_MINUTES} minutes` };
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
 *  in the backend), and a runId cannot be looked up anywhere. */
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
 *  host's event subscription and child messages still steer the agent, and the
 *  first such message ends dormancy (wake). The director also says how long to
 *  sleep: when that runs out without input, dormancy ends and the review it parked
 *  runs once (`notify('dormancy_timeout', was)`). On the first idle after entering it,
 *  `compact` runs once - the provider cache will be cold by the next wake anyway,
 *  so a large context is cheapest to shrink while it is still warm. `changed` is
 *  told whenever dormancy starts or ends, so the reported phase follows it. */
export class IdleReview {
  constructor({ agent, goals, notify, onError, compact, continuation, ask, canContinue = () => true, changed = () => {}, intervalMs = 15 * 60 * 1000 }) {
    Object.assign(this, { agent, goals, notify, onError, compact, continuation, ask, canContinue, changed, intervalMs });
    this.closed = false;
  }
  enterDormancy(reason, wakeAfterMs) {
    clearTimeout(this.timer);
    clearTimeout(this.dormantTimer);
    const since = new Date();
    this.dormant = { reason, since: since.toISOString(), until: new Date(since.getTime() + wakeAfterMs).toISOString(), compactPending: true };
    this.dormantTimer = setTimeout(() => this.timedOut(), wakeAfterMs);
    this.dormantTimer.unref?.();
    this.changed();
    return this.dormant;
  }
  /** Ends dormancy; returns what it was (undefined when not dormant). */
  wake() {
    const was = this.dormant;
    clearTimeout(this.dormantTimer);
    this.dormant = undefined;
    if (was) this.changed();
    return was;
  }
  /** The sleep the director chose ran out with no input: run the parked review now. */
  timedOut() {
    if (this.closed) return;
    const was = this.wake();
    if (!was || this.agent.status !== 'idle' || this.goals.get(this.agent)?.phase !== 'active') return;
    Promise.resolve().then(() => this.notify('dormancy_timeout', was)).catch(this.onError);
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
  close() { this.closed = true; clearTimeout(this.timer); clearTimeout(this.dormantTimer); }
}

/** The host-side todo plan store, reached only through `python -m <module>`.
 *  Predicates, freezing and evaluation live in Python; this only shells out.
 *  `module` is the CLI module and `stateDirEnv` the variable it reads its state
 *  directory from; the host names both. */
export class TodoCli {
  constructor({ python, cwd, stateDir, module, stateDirEnv }) {
    Object.assign(this, { python, cwd, stateDir, module, stateDirEnv });
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
      const child = spawn(this.python, ['-m', this.module, ...args], { cwd: this.cwd, detached: true,
        stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, [this.stateDirEnv]: this.stateDir } });
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
 *  after its last `todo_done`: the plan has no `remaining` items, the work item
 *  is still live and has no candidate report, and `todo_finish` has not
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
  // A batch sub-plan (it names its `batch`) is never finished on its own: its batch
  // attempt is, and `list` reports that attempt as its own row once everything passed.
  return !plan.batch && Array.isArray(plan.remaining) && plan.remaining.length === 0 && plan.finish_reported !== true;
}

/** The plan attempt ids whose work item the host says has SETTLED.
 *
 *  One `workSettled(id)` for every plan a view could show - one that still has
 *  items, and one whose items have all passed but which was never finished.
 *  `workSettled` answers true only when the host can prove the work item settled.
 *  FAILS CLOSED: a call that throws (host unreachable, an unknown id) or answers
 *  anything but true leaves that attempt OUT of the set, so its plan keeps
 *  blocking. This set only ever shrinks the blocking list by what the host said. */
export async function settledAttempts(plans, workSettled, signal) {
  const settled = new Set();
  // A batch sub-plan is named `<attempt>~<stage>-<key>`, which the host has never heard
  // of: it settles with the attempt its `batch` names, read once per attempt.
  const reads = new Map();
  const attemptRead = id => {
    if (!reads.has(id)) reads.set(id, workSettled(id, signal));
    return reads.get(id);
  };
  await Promise.all(plans.filter(plan => plan.remaining?.length || awaitingFinish(plan)).map(async plan => {
    let answer;
    try { answer = await attemptRead(plan.batch ?? plan.attempt_id); }
    catch { return; /* fail closed: an unread plan is unfinished work, not settled */ }
    if (answer === true) settled.add(plan.attempt_id);
  }));
  return settled;
}

/** The batch attempts the startup snapshot must ask for `batch_redispatch`: those
 *  with a sub-plan that still has items left and is bound to a session that is
 *  not live (a host restart kills every native child). Settled attempts are
 *  history, not work. */
export function batchesToRedispatch(plans, liveSessions, settled = new Set()) {
  return [...new Set(plans.filter(p => p.batch && !settled.has(p.attempt_id) && p.remaining?.length
    && p.child_session && !liveSessions.has(p.child_session)).map(p => p.batch))].sort();
}

/** The snapshot's `batch_redispatch`: every batch sub-plan whose bound session is
 *  not live and that has not passed, with the prompt to dispatch it again, as the
 *  CLI's `batch-status` computes it against the same live sessions. */
export async function batchRedispatchRows(todo, plans, live, settled = new Set(), signal) {
  const rows = [];
  for (const attempt of batchesToRedispatch(plans, new Set(live), settled)) {
    const { code, result } = await todo.run(['batch-status', '--attempt=' + attempt, '--live-sessions=' + JSON.stringify([...live])], signal);
    if (code !== 0) throw new Error('todo_cli batch-status refused: ' + JSON.stringify(result).slice(0, 1000));
    rows.push(...result.redispatch.map(row => ({ attempt_id: attempt, ...row })));
  }
  return rows;
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
 *  blocker; a plan whose attempt the host could not answer for stays one.
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

/** The event types a history page shows (moved from dsh-driver/history.mjs). */
export const visibleTypes = new Set(['user/message', 'assistant/message', 'tool/call', 'tool/result', 'compaction/end', 'agent/error']);

/** One page of a session's raw history, newest last (moved from dsh-driver/history.mjs). */
export function historyPage(events, { before_seq = Number.MAX_SAFE_INTEGER, limit = 50 } = {}) {
  const visible = visibleTypes;
  const selected = events.filter(e => e.seq < before_seq && visible.has(e.type));
  const page = selected.slice(-limit);
  return { entries: page.map(e => {
    const text = JSON.stringify(e.data, null, 2);
    return { seq: e.seq, type: e.type, name: e.data?.name ?? null, timestamp: e.time ?? null, text: text.slice(0, 16000), truncated: text.length > 16000 };
  }), has_more: selected.length > page.length, next_before_seq: page[0]?.seq ?? null };
}
