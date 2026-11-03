/** A resident State DAG driver composed over the pinned DSH base profile. */
import { historyPage } from './history.mjs';
import { installHostModel } from './model.mjs';
import { createRequire } from 'node:module';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'aitelier-state-driver';
export const inject = ['agents', 'sessions', 'sessionPersistence', 'sessionQuery', 'systemPrompt', 'tools', 'compaction', 'credentials', 'llm'];

const goalTemplate = await readFile(new URL('./goal.md', import.meta.url), 'utf8');
export const driverGoal = project => goalTemplate.replaceAll('{{project_id}}', project);
export function connectionErrorMessage(error) {
  const cause = error.cause;
  return `AItelier event subscription: ${error.message}${cause ? ` (${[cause.code, cause.message].filter(Boolean).join(': ')})` : ''}`.slice(0, 2000);
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
  constructor({ project, driver, directory, request, onEvent = () => {}, onConnectionError = () => {}, onConnectionRestored = () => {}, retryDelayMs = 1000, pendingInputs = () => [] }) {
    Object.assign(this, { project, driver, directory, request, onEvent, onConnectionError, onConnectionRestored, retryDelayMs, pendingInputs });
    this.after = 0;
    this.inboxAfter = 0;
    this.snapshot = null;
    this.dirty = true;
    this.waiters = new Set();
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
    this.dirty = false;
    return this.snapshot;
  }
  render() {
    return JSON.stringify({ project_id: this.project, driver_id: this.driver,
      driver_goal: driverGoal(this.project),
      nodes: this.snapshot.nodes, project_note: this.snapshot.project_note,
      private_note: this.snapshot.private_note, driver_skill: this.snapshot.driver_skill,
      runtime_skill: this.snapshot.runtime_skill });
  }
  wake() {
    // A notification is an edge, not durable work. DSH owns the pending input.
    for (const resolve of this.waiters) resolve();
  }
  async wait(signal) {
    while (true) {
      if (signal?.aborted) throw signal.reason;
      if (this.controller.signal.aborted) throw this.controller.signal.reason;
      const pending = this.pendingInputs();
      if (pending.length) return pending;
      await new Promise((resolve, reject) => {
        const done = () => { cleanup(); resolve(); };
        const abort = () => { cleanup(); reject(signal?.aborted ? signal.reason : this.controller.signal.reason); };
        const cleanup = () => {
          this.waiters.delete(done);
          signal?.removeEventListener('abort', abort);
          this.controller.signal.removeEventListener('abort', abort);
        };
        this.waiters.add(done);
        signal?.addEventListener('abort', abort, { once: true });
        this.controller.signal.addEventListener('abort', abort, { once: true });
      });
    }
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
  if (!hostExecution) ctx.systemPrompt.context({ name: 'aitelier:project-work', order: 5, interpolate: false,
    text: `Project ${project}: project_work is available to the driver and all native subagents. Parent assignments must include the registered State attempt_id and current owner fence. Each caller gets its own owned worktree: open, then read/write/edit/bash there, verify, then deliver (commit_message when needed). Only deliver writes back to Source. push(expected_head) uses the project's actual origin without force. bash runs in the existing isolated test container; do not run repository tests in the production backend. Use native local tools for orchestration/inbox/history, not as a fallback for denied project work. No new planning gate is required.` });
  if (hostExecution) ctx.systemPrompt.context({ name: 'aitelier:host-work', order: 5, interpolate: false,
    text: `Project ${project}: you and your native DSH subagents run on the Linux host with danger-full-access and approval never. Use native file and bash tools directly for coding, Git fetch/merge/commit/push, Docker and installed agent CLIs. project_work is not mounted in this runtime. Existing history or notes describing workspace-write, read-only Git or missing Docker describe the previous container runtime. Read the current State source binding for the repository; the backend binding /app maps to host ${env.AITELIER_DSH_BACKEND_SOURCE ?? 'the AItelier deployment checkout'}, all other absolute bindings retain their host path. Each writer needs its own checkout/worktree, each run one controller. Run AItelier pytest only in disposable containers: docker run --rm --network none --cpus 2 --memory 2g, one worker, at most four concurrent; never directly on the host or in the production container. Use your own driver token for AItelier calls. Full access does not change project rules or grant permission to publish private source publicly.` });
  const runtime = new DriverRuntime({ project, driver: env.AITELIER_DRIVER_ID,
    directory: env.AITELIER_DSH_STATE_DIR, request });
  await runtime.init();
  const schema = await request('/api/state/schema');
  const subagents = new Set();
  let agent, agentCtx, waiting = false, disposed = false;
  let activity = Promise.resolve();
  const report = (phase, error = '', event) => {
    const body = { phase, subagents: [...subagents], error, ...(event ? { event } : {}) };
    activity = activity.catch(() => {}).then(() => request(`/api/dsh-drivers/${encodeURIComponent(project)}/activity`,
      body));
    activity.catch(error => ctx.logger.warn(String(error)));
    return activity;
  };
  const message = text => createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } });
  const projectInbox = () => request('/api/state/query/list_director_messages', {
    project_id: project, protocol_version: 'v3', statuses: ['unread', 'acknowledged'], limit: 20,
  });
  runtime.onConnectionError = async error => {
    const detail = connectionErrorMessage(error);
    ctx.logger.warn(detail);
    await report(waiting ? 'waiting' : 'running', detail).catch(() => {});
  };
  runtime.onConnectionRestored = () => report(waiting ? 'waiting' : 'running');
  runtime.onEvent = async result => {
    // Steer at the next step: a resident turn can remain in wait() indefinitely.
    // Queue into the durable DSH inbox before committing the State cursor. An
    // interrupted delivery is replayed, never silently skipped.
    const inbox = result.events?.some(e => e.event_type === 'director_message_received') ? await projectInbox() : undefined;
    agent.steer(message('AItelier runtime event:\n' + JSON.stringify({ ...result, project_inbox: inbox })));
    await ctx.sessionPersistence.flush();
  };
  const setup = async (scope, subject) => {
    agentCtx = scope;
    installModelSelection(scope, { current: { provider: env.AITELIER_DSH_PROVIDER,
      model: env.AITELIER_DSH_MODEL }, assembled: undefined });
    scope.systemPrompt.context({ name: 'aitelier:state', order: 0, required: true,
      interpolate: false, text: () => runtime.render() });
    scope.on('agent/pre-step', async (_payload, next) => {
      const decision = await next(); // Includes the base automatic compactor.
      if (runtime.dirty) await runtime.refresh();
      return decision;
    });
    // Overflow recovery may rebuild a request without another pre-step. Refresh
    // the required context at the assembly boundary as well.
    scope.on('system-prompt/assemble', async (assembly, _context, next) => {
      if (runtime.dirty) await runtime.refresh();
      const value = await next();
      const state = value.contexts.find(c => c.name === 'aitelier:state');
      if (state) state.text = runtime.render();
      return value;
    });
    runtime.pendingInputs = () => subject.inbox.nextStep.map(input => ({
      reason: 'native_input_pending', message_id: input.id, source_kind: input.source.kind,
    }));
    scope.on('agent/inbox/inserted', () => runtime.wake());
    scope.tools.register(defineTool({ name: 'wait',
      description: 'Wait for pending native input from State, driver inbox or subagents. Returns queued message IDs; the next step includes those messages. Runtime owns subscriptions and timeouts.',
      parameters: {},
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(_args, exec) {
        waiting = true; await report('waiting');
        try { return await runtime.wait(exec.signal); }
        finally { waiting = false; if (!disposed) await report('running'); }
      },
    }));
    // The external-client waits stay on the server. The resident driver sees
    // only wait(), including when it works through PTC mode.
    const hidden = ['state_graph_read', 'wait_for_run', 'wait_for_driver_inbox']
      .map(n => 'mcp__aitelier__' + n).filter(n => scope.tools.get(n));
    if (hidden.length) scope.tools.restrict({ deny: hidden });
    scope.tools.register(defineTool({ name: 'state_read',
      description: 'Query State. Use state_graph_help for action schemas. Current project_id is supplied when omitted. Use wait() for event waits.',
      parameters: { action: { type: 'string', required: true }, arguments: { type: 'object', additionalProperties: true } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(args, exec) {
        if (['wait_for_state_change', 'wait_for_driver_inbox'].includes(args.action)) throw new Error('Use wait()');
        const arguments_ = { ...args.arguments };
        if (!arguments_.project_id && !arguments_.driver_id && schema.operations[args.action]?.arguments?.properties?.project_id) arguments_.project_id = project;
        return request('/api/state/query/' + encodeURIComponent(args.action), arguments_, exec.signal);
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
  if (hostExecution) {
    // Resume preserves old pinned policy. Change it through DSH's durable API;
    // children inherit this explicit full-access selection, including forks.
    ctx.get('permissionPresets').set(agent.session, 'danger-full-access');
  }
  await ctx.sessionPersistence.flush();
  ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return;
    if (event.type === 'compaction/end' && !event.data.error) runtime.dirty = true;
    const entry = historyPage([event], { limit: 1 }).entries[0];
    if (entry) void report(waiting ? 'waiting' : 'running', '', entry);
  });
  // Observe nested native children too: all activity belongs to this private
  // per-DAG process, so no cross-project worker can enter these counts.
  ctx.on('subagent/start', info => { subagents.add(info.runId); void report(waiting ? 'waiting' : 'running'); });
  ctx.on('subagent/end', info => { subagents.delete(info.runId); void report(waiting ? 'waiting' : 'running'); });
  ctx.on('agent/error', ({ agent: subject, error }) => { if (subject === agent) void report('failed', String(error).slice(0, 2000)); });
  const close = async () => {
    if (disposed) return;
    disposed = true; runtime.close();
    await handle.dispose();
    await ctx.sessionPersistence.flush();
    await activity.catch(() => {});
  };
  ctx.on('dispose', close);
  await report('running');
  const inbox = await projectInbox();
  agent.followup(message('Resume the assigned State DAG from the current injected state. Follow driver_goal as this project’s standing director mandate. Read relevant contracts, notes and both inboxes. Review issues and unfinished delivery as well as the frontier, dispatch actionable work, and wait() only after assessing remaining work and concrete blockers.\nPending project inbox:\n' + JSON.stringify(inbox)));
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
