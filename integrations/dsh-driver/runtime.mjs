/** A resident State DAG driver composed over the pinned DSH base profile. */
import { historyPage } from './history.mjs';
import { installHostModel } from './model.mjs';
import { installToolDiscovery } from './tool-discovery.mjs';
import { bindDriverGoal, driverGoalSection } from './goal.mjs';
import { createRequire } from 'node:module';
import { readFile, writeFile, rename, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'aitelier-state-driver';
export const inject = ['agents', 'sessions', 'sessionPersistence', 'sessionQuery', 'systemPrompt', 'tools', 'compaction', 'credentials', 'llm', 'goals'];

const goalTemplate = await readFile(new URL('./goal.md', import.meta.url), 'utf8');
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

/** The resident runtime owns idle time; the model never holds a wait tool open. */
export class IdleReview {
  constructor({ agent, goals, notify, onError, intervalMs = 15 * 60 * 1000 }) {
    Object.assign(this, { agent, goals, notify, onError, intervalMs });
    this.closed = false;
  }
  statusChanged(status) {
    clearTimeout(this.timer);
    if (this.closed || status !== 'idle') return;
    // Keep the durable mandate active, while parking immediate native rounds.
    // Incoming steering and native child messages still wake an idle agent.
    this.goals.disarm(this.agent);
    if (this.goals.get(this.agent)?.phase !== 'active') return;
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
  installHostModel(ctx, llm, { request, project: env.AITELIER_DSH_PROJECT, session: env.AITELIER_DSH_SESSION });
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
  let agent, agentCtx, waiting = false, disposed = false;
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
  const idleReview = new IdleReview({ agent, goals: ctx.goals,
    ...(Number(env.AITELIER_DSH_IDLE_REVIEW_MS) > 0 ? { intervalMs: Number(env.AITELIER_DSH_IDLE_REVIEW_MS) } : {}),
    notify: async () => {
      let todoView;
      try { todoView = { todo_unfinished: unfinishedWithoutChild(await todo.plans(), liveChildren) }; }
      catch (error) { ctx.logger.warn(String(error)); todoView = { todo_error: String(error.message ?? error).slice(0, 1000) }; }
      await notify('AItelier director review due:\n' + JSON.stringify({
        project_id: project, reason: 'idle_review_due',
        instruction: 'Recheck live State frontier, open issues, worker capacity and prior blockers. Continue independent authorized work; a blocked task does not block the whole project. If no action is available, finish this round and remain subscribed.',
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
  const goal = bindDriverGoal(ctx.goals, agent, objective, { onSeed: view => { seeded = view; } });
  if (seeded) {
    const sha256 = createHash('sha256').update(objective).digest('hex');
    ctx.logger.info(`director goal ${seeded.id} re-seeded from goal.md (sha256 ${sha256.slice(0, 12)}, revision ${seeded.revision})`);
    const file = join(env.AITELIER_DSH_STATE_DIR, 'goal-seed.json');
    await writeFile(file + '.tmp', JSON.stringify({ sha256, goal_id: seeded.id, revision: seeded.revision, at: new Date().toISOString() }), { mode: 0o600 });
    await rename(file + '.tmp', file);
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
