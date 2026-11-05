/** A resident State DAG driver composed over the pinned DSH base profile. */
import { historyPage } from './history.mjs';
import { installHostModel, rootReasoningEffort } from './model.mjs';
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
 *  budget of MAX_AUTO_CONTINUE questions, which bounds a director that only polls. */
export class Continuation {
  constructor({ maxAuto = MAX_AUTO_CONTINUE } = {}) {
    this.maxAuto = maxAuto;
    this.auto = 0;
    this.tools = 0;
    this.text = '';
  }
  observe(event) {
    if (event.type === 'tool/call') this.tools++;
    else if (event.type === 'assistant/message') this.text = (event.data?.message?.content ?? [])
      .filter(block => block.type === 'text').map(block => block.text ?? '').join('\n');
    else if (endsDormancy(event)) this.auto = 0;
  }
  /** At idle: true to ask once more, false to go idle. Always starts a new turn record. */
  turnEnded() {
    const worked = this.tools > 0 && !this.text.includes(NOTHING_LEFT);
    this.tools = 0;
    this.text = '';
    if (!worked || this.auto >= this.maxAuto) return false;
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
 *  without a live child each refuse. claims/plans are omitted for the early check. */
export function dormancyRefusal({ reason, liveChildren = 0, claims = [], plans = [], background = [] }) {
  if (!reason) return { refused: 'reason_required', reason: 'say why no work is available' };
  if (liveChildren) return { refused: 'live_children', reason: `${liveChildren} native child(ren) still running` };
  if (claims.length) return { refused: 'live_claims', reason: 'you hold live State claims', node_keys: claims.map(c => c.node_key) };
  if (plans.length) return { refused: 'unfinished_plans', reason: 'finish, report or hand back these plans first', attempts: plans.map(p => p.attempt_id) };
  if (background.length) return { refused: 'background_processes', reason: 'jobs you started are still running; watch_process them or wait', pids: background };
  return null;
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
  /** Every stored plan's summary (`list`). No store yet means no plan was ever
   *  made, so the interpreter is not started for it. */
  async plans(signal) {
    try { await stat(this.store); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    const { code, result } = await this.run(['list'], signal);
    if (code !== 0) throw new Error('todo_cli list refused: ' + JSON.stringify(result).slice(0, 1000));
    return result.plans.filter(plan => !plan.error);
  }
}

/** The post-compaction snapshot's view of unfinished plans: enough to resume, not the items. */
export function todoSummary(plans, liveSessions) {
  return plans.filter(p => p.remaining?.length).map(p => ({ attempt_id: p.attempt_id, node_key: p.node_key, next: p.next,
    remaining_count: p.remaining?.length ?? 0, child_alive: liveSessions.has(p.child_session) }));
}

/** Plans the idle review must hand back: unfinished, and no live child on them. */
export function unfinishedWithoutChild(plans, liveSessions) {
  return plans.filter(p => p.remaining?.length && !liveSessions.has(p.child_session))
    .map(p => ({ attempt_id: p.attempt_id, node_key: p.node_key, next: p.next, remaining: p.remaining }));
}

/** Whether the director may take a plan over from a named session, or why not.
 *
 *  A host restart can kill a native child mid-plan: the plan stays bound to a
 *  session that no longer exists, so `todo_done` answers `bound_to_other_session`
 *  forever and `todo_finish` refuses (`never begun`), which in turn keeps
 *  `idle_dormant` refusing `unfinished_plans`. The exit is one deliberate
 *  takeover, and its whole safety is the liveness check: only the session the
 *  plan is actually bound to may be replaced, and only while it is provably not
 *  running. A live child keeps its plan and keeps answering `bound_to_other_session`.
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
    return JSON.stringify({ project_id: this.project, driver_id: this.driver,
      nodes: this.snapshot.nodes, project_note: this.snapshot.project_note,
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
  installHostModel(ctx, llm, { request, project: env.AITELIER_DSH_PROJECT, session: env.AITELIER_DSH_SESSION,
    rootEffort: rootReasoningEffort(env.AITELIER_DSH_ROOT_REASONING_EFFORT) });
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
  const liveChildren = new Set();
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
      if (!bound || bound === env.AITELIER_DSH_SESSION || liveChildren.has(bound)) return result;
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
  const runtime = new DriverRuntime({ project, driver: env.AITELIER_DRIVER_ID,
    directory: env.AITELIER_DSH_STATE_DIR, request, todoPlans: async () => todoSummary(await todo.plans(), liveChildren) });
  await runtime.init();
  const schema = await request('/api/state/schema');
  const subagents = new Set();
  const watchIntervalMs = Number(env.AITELIER_DSH_WATCH_INTERVAL_MS) > 0 ? Number(env.AITELIER_DSH_WATCH_INTERVAL_MS) : 15000;
  const floorEnv = Number(env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS);
  const compactFloor = env.AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS !== undefined && Number.isFinite(floorEnv) && floorEnv >= 0
    ? floorEnv : DORMANT_COMPACT_MIN_TOKENS;
  // idleReview is created after the agent; tools reach it only when called.
  let agent, agentCtx, idleReview, waiting = false, disposed = false;
  const continuation = new Continuation();
  // watch_process: pid -> { note, since }. One poller while any watch exists.
  const watches = new Map();
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
    const body = { phase, subagents: [...subagents], error, ...(event ? { event } : {}) };
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
  const projectInbox = () => request('/api/state/query/list_director_messages', {
    project_id: project, protocol_version: 'v3', statuses: ['unread', 'acknowledged'], limit: 20,
  });
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
        body: { type: 'string', required: true }, node_key: { type: 'string' } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(args, exec) {
        const { node_key, ...rest } = args;
        const body = { ...rest, project_id: project };
        if (node_key) body.refs = { node_key };
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
          '--out=' + join(todo.store, 'finish', args.attempt_id)], exec.signal,
          await todo.bound(args.attempt_id, status => status.finish_budget_seconds, exec.signal))
        : { refused: 'invalid_attempt_id', reason: 'attempt_id must be a plain file name', brief: null },
    }));
    // The escape hatch for a plan whose child died mid-item (a host restart
    // kills it): the director names the dead session and takes the item over.
    // Without this an item already begun by a dead session has no exit at all -
    // todo_done answers bound_to_other_session, todo_finish refuses, and
    // idle_dormant refuses unfinished_plans forever.
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
          bound_session: status.child_session, live: [...liveChildren], root_session: root });
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
      description: 'Director only. Call when no authorized work is available and nothing is in flight: stops the 15-minute idle review (no model turns while nothing happens) and compacts a large context once. You stay subscribed: any State event, inbox message, owner message or child message wakes you and the review resumes. Refused while you have a live child, a live State claim or an unfinished todo plan.',
      parameters: { reason: { type: 'string', required: true } }, output: jsonOutput,
      async execute(args, exec) {
        const reason = typeof args.reason === 'string' ? args.reason.trim() : '';
        const early = dormancyRefusal({ reason, liveChildren: liveChildren.size });
        if (early) return early;
        let claims, plans;
        try {
          const listed = await request('/api/state/query/list_claims', { project_id: project, driver_id: env.AITELIER_DRIVER_ID, limit: 100 },
            AbortSignal.any([exec.signal, AbortSignal.timeout(30000)]));
          claims = ((listed.result ?? listed).claims ?? []).filter(c => c.status === 'live' && c.driver_id === env.AITELIER_DRIVER_ID);
          plans = unfinishedWithoutChild(await todo.plans(exec.signal), liveChildren);
        } catch (error) {
          return { refused: 'unverifiable', reason: 'could not prove nothing is in flight: ' + String(error.message ?? error).slice(0, 500) };
        }
        const background = backgroundProcesses(env.AITELIER_DSH_SESSION).filter(pid => !watches.has(pid));
        const refusal = dormancyRefusal({ reason, liveChildren: liveChildren.size, claims, plans, background });
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
      try { todoView = { todo_unfinished: unfinishedWithoutChild(await todo.plans(), liveChildren) }; }
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
  ctx.on('subagent/start', info => { subagents.add(info.runId); liveChildren.add(info.id); void report(currentPhase()); });
  ctx.on('subagent/end', info => {
    subagents.delete(info.runId); liveChildren.delete(info.id); void report(currentPhase());
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
  const pendingInbox = (inbox.result ?? inbox).items?.length;
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
