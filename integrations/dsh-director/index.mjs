/** dsh-director: a resident director and its native workers, as a DSH plugin.
 *
 *  What it owns (no backend needed): the director/worker role table and tool tiers,
 *  todo plans and batches, the continuation check, idle review and dormancy with a
 *  chosen wake time, process watches, live-children bookkeeping, child handback and
 *  history grep. What it does NOT own: where work comes from and where it is
 *  recorded. That is the host's, reached only through the `directorHost` cordis
 *  service (lib/host.mjs, host.d.ts) - the backend adapter is one host, the
 *  local-files reference host another.
 *
 *  Moved from integrations/dsh-driver/runtime.mjs boot() (plugin design step 2):
 *  every line below is the old code, except where it now asks the host for what
 *  used to be a backend call. Order of registration is kept, because the order of
 *  tools in a request is part of the request (design §9 MF2). */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { installToolDiscovery } from './lib/tool-discovery.mjs';
import { loadRoles, roleOf, validateEffectiveRoles } from './lib/roles.mjs';
import { validateHost, optionalDefaults, isSettled } from './lib/host.mjs';
import { defaultLabels, defaultTexts, ContextInjection, connectionErrorMessage, wakeSourcesFor, endsDormancyFor,
  Continuation, processSession, processAlive, backgroundProcesses, watchRefusal, dormancyRefusal, DORMANT_MIN_MINUTES,
  DORMANT_MAX_MINUTES, wakeAfterRefusal, LiveChildren, phaseReport, continuationWaitKey, DORMANT_COMPACT_MIN_TOKENS,
  IdleReview, TodoCli, settledAttempts, batchRedispatchRows, todoSummary, unfinishedWithoutChild, rebindRefusal,
  grepHistory, historyPage } from './lib/core.mjs';
import { Outbox } from './lib/outbox.mjs';
import { capacityLimits, describeDispatch, isDshLimitRefusal, RefusedDispatches } from './lib/capacity.mjs';
import { lastTurn, resumeCandidates, refusal, recoveryNote, ResumeRecords } from './lib/resume.mjs';
import { createHash } from 'node:crypto';
import { openMemory, profileSection, registerMemoryTools } from './lib/memory/tools.mjs';
import { registerPortfolioTools } from './lib/portfolio.mjs';
import { registerProposalTools } from './lib/proposals.mjs';

export const name = 'dsh-director';
export const inject = ['agents', 'sessions', 'sessionPersistence', 'sessionQuery', 'systemPrompt', 'tools', 'compaction',
  'credentials', 'llm', 'goals', 'tokenMeter', 'directorHost'];

/** The texts and labels a host runs with: the defaults, overridden by the host's. */
export function hostWording(host) {
  const base = defaultTexts();
  const texts = { ...base, ...(host.texts ?? {}), tools: { ...base.tools, ...(host.texts?.tools ?? {}) } };
  const labels = { ...defaultLabels, ...(host.labels ?? {}) };
  return { texts, labels };
}

export async function boot(ctx, { host = ctx.directorHost } = {}) {
  // Anything a session log wrote from here on, this process wrote (plugin step 6).
  const startedAt = Date.now();
  validateHost(host);
  const config = host.config;
  const { texts, labels } = hostWording(host);
  const op = name => host[name] ? host[name].bind(host) : optionalDefaults[name];
  const sessionEnv = config.sessionEnv;
  await ctx.get('loader')?.await();
  const require = createRequire(config.dshEntry);
  const { defineTool } = await import(require.resolve('@deepseek-ai/dsh-tools'));
  const llm = await import(require.resolve('@deepseek-ai/dsh-llm'));
  const { createUserMessage } = llm;
  // roles() is synchronous and must return a valid table (the loaded one, extended):
  // a Promise or a broken table is refused here, naming it, not met later as a crash.
  const roles = op('roles')(loadRoles(config.rolesFile));
  if (roles && typeof roles.then === 'function') throw new Error('directorHost refused: roles(table) must return the table synchronously, not a Promise');
  validateEffectiveRoles(roles);
  // Plugin step 12: the root session's role - the director, or the steward when the host
  // says so (validateHost checked the name; the table must have that role).
  const rootRole = config.rootRole ?? 'director';
  if (!roles[rootRole]) throw new Error(`directorHost refused: config.rootRole is ${rootRole} but the role table has no ${rootRole}`);
  const promptDir = dirname(config.rolesFile);
  const runtimeSkill = readFileSync(join(promptDir, roles[rootRole].prompt), 'utf8');
  const workerSkill = readFileSync(join(promptDir, roles.worker.prompt), 'utf8');
  installToolDiscovery(ctx, { defineTool, HarnessError: llm.HarnessError, rootSession: config.sessionId, rootRole, roles,
    texts: { sectionName: labels.sectionPrefix + ':tool-discovery', section: texts.toolDiscoverySection,
      searchDescription: texts.tools.search_tools } });
  // The worker protocol reaches every native child and never the director (whose
  // prompt is a root-scope section in setup). A caller-less assembly gets neither.
  ctx.systemPrompt.section({ name: labels.sectionPrefix + ':worker-protocol', order: 12, interpolate: false, text: context => {
    const id = (context?.agent ?? context?.scope)?.session?.id;
    return id !== undefined && roleOf(id, config.sessionId, rootRole) === 'worker' ? workerSkill : '';
  } });
  const todo = new TodoCli({ python: config.todo.python ?? 'python3', cwd: config.todo.cwd, stateDir: config.stateDir,
    module: config.todo.module, stateDirEnv: config.todo.stateDirEnv });
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
  // Worker capacity (plugin step 9, lib/capacity.mjs): DSH refuses a new child at its
  // maxActiveSubagents without queueing. The director is refused one step earlier -
  // `reserved` (>= 1) slots stay free for resuming workers and follow-up messages - with
  // a structured answer naming what was refused, and never above DSH's own limit (read
  // live: it is a volatile setting). Refusals are kept until the slot-free wake that
  // names them was delivered (through the outbox), across a restart too.
  const dshLimit = () => ctx.get('subagents')?.config?.maxActiveSubagents?.get?.();
  const limits = () => capacityLimits({ hostLimit: config.maxActiveSubagents, dshLimit: dshLimit(), reservedSlots: config.reservedSlots });
  const refusals = await new RefusedDispatches(config.stateDir, line => ctx.logger.warn(line)).load();
  const warn = error => ctx.logger.warn(String(error));
  // Dispatches the guard let through whose result has not come back yet. Parallel calls
  // in one message are all guarded before any child starts, so live children alone
  // would let a batch run past the reserve (measured: 4 parallel calls at cap 3 started
  // 3). Counting them too may over-count a child for the moment between its start and
  // its result; it never under-counts. A spawn answers within seconds, so an entry
  // older than two minutes is a result that never came, and stops counting.
  const admitted = new Map();
  const inFlight = () => {
    for (const [callId, since] of admitted) if (Date.now() - since > 120000) admitted.delete(callId);
    return admitted.size;
  };
  ctx.tools.guard(exec => {
    if (exec.name !== 'subagent' || exec.agent?.session?.id !== config.sessionId) return undefined;
    pruneChildren();
    const live = children.size() + inFlight(), { threshold, ...limit } = limits();
    if (live < threshold) { admitted.set(exec.callId ?? Symbol('call'), Date.now()); return undefined; }
    const dispatch = describeDispatch(exec.arguments);
    void refusals.add(dispatch, 'plugin').catch(warn);
    return JSON.stringify({ refused: 'at_capacity', live, ...limit, dispatch,
      reason: texts.atCapacity ?? `every worker slot but the ${limit.reserved} reserved one(s) is in use (${live} of ${limit.capacity}); this worker was not started. You get one runtime event naming it when a slot frees: dispatch it then, and meanwhile continue other work or wait.` });
  });
  // DSH refused the dispatch itself (a race the guard could not see): the same refusal.
  ctx.on('tools/result', (exec, result) => {
    if (exec?.name !== 'subagent' || exec.agent?.session?.id !== config.sessionId) return;
    admitted.delete(exec.callId);
    if (!isDshLimitRefusal(result)) return;
    void refusals.add(describeDispatch(exec.arguments), 'dsh').catch(warn);
  });
  const jsonOutput = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
  // Plugin step 11: the owner's memory, only when the host config asks for it. A memory
  // that cannot start is {error}: its tools answer that, everything else runs.
  const memory = config.memory ? await openMemory(config.memory, { sessionId: config.sessionId,
    logger: (event, fields) => ctx.logger.info(event + ' ' + JSON.stringify(fields)) }) : undefined;
  // The plans, and what the host says about the work item each one is named after.
  // The two are read together because every view of "unfinished work" needs both: a
  // plan whose work has settled is not work, and one whose work the host could not
  // answer for still is (settledAttempts fails closed).
  const workState = op('workState');
  const workSettled = async (id, signal) => isSettled(await workState(id, signal));
  const planWork = async signal => {
    const plans = await todo.plans(signal);
    return { plans, settled: await settledAttempts(plans, workSettled, signal) };
  };
  // The sessions a batch sub-plan may be bound to and still count as in flight: the
  // live native children, and the director itself (todo_rebind binds the root).
  const liveSessions = () => [...children.sessionSet(), config.sessionId].filter(Boolean);
  // Batch sub-plans whose child is gone, each with the prompt to dispatch it again.
  const batchRedispatch = async () => {
    const { plans, settled } = await planWork();
    return batchRedispatchRows(todo, plans, liveSessions(), settled);
  };
  const todoPlans = async () => { const { plans, settled } = await planWork();
    return todoSummary(plans, children.sessionSet(), settled); };
  const watchIntervalMs = Number(config.watchIntervalMs) > 0 ? Number(config.watchIntervalMs) : 15000;
  const floorValue = config.dormantCompactMinTokens;
  const floorNumber = Number(floorValue);
  const compactFloor = floorValue !== undefined && Number.isFinite(floorNumber) && floorNumber >= 0
    ? floorNumber : DORMANT_COMPACT_MIN_TOKENS;
  // Tests shrink a minute; production never sets it.
  const dormantMinuteMs = Number(config.dormantMinuteMs) > 0 ? Number(config.dormantMinuteMs) : 60 * 1000;
  // idleReview is created after the agent; tools reach it only when called.
  let agent, agentCtx, idleReview, waiting = false, disposed = false;
  // watch_process: pid -> { note, since }. One poller while any watch exists.
  const watches = new Map();
  const endsDormancy = endsDormancyFor(wakeSourcesFor(labels));
  // What the director is waiting on that will wake it: its live children and watched
  // jobs. `continuationWaitKey` reconciles the children, so a departed one leaves the
  // key - a synchronous read of a synchronous oracle, no await needed or available here.
  const continuation = new Continuation({ endsDormancy, waitKey: () => continuationWaitKey({ children,
    watched: [...watches.keys()].sort((a, b) => a - b) }) });
  let watchTimer;
  const pollWatches = () => {
    for (const [pid, watch] of watches) {
      if (processAlive(pid) && processSession(pid, '/proc', sessionEnv) === config.sessionId) continue;
      watches.delete(pid);
      void outbox.send(`watch:${pid}:${watch.since}`, texts.jobFinished + JSON.stringify({ pid, note: watch.note,
        ran_seconds: Math.round((Date.now() - watch.since) / 1000) })).catch(error => ctx.logger.warn(String(error)));
    }
    if (!watches.size) { clearInterval(watchTimer); watchTimer = undefined; }
  };
  let hostError = '', completionError = '';
  let activity = Promise.resolve();
  const progress = op('progress');
  // Dormant is still idle, reported apart so a dashboard can say "asleep", not "idle".
  const currentPhase = () => waiting ? completionError ? 'failed' : idleReview?.dormant ? 'dormant' : 'waiting' : 'running';
  const report = (phase, error = completionError || hostError, event) => {
    // One report per call, in order; `phaseReport` names the live child SESSIONS,
    // reconciled against the registry, never the runIds the registry cannot answer for.
    const body = phaseReport({ phase, children, error, event });
    activity = activity.catch(() => {}).then(() => progress(body));
    activity.catch(error => ctx.logger.warn(String(error)));
    return activity;
  };
  // Two distinct producers, two distinct kinds. A host event is NOT the user
  // speaking: labelling both `user` made a driver unable to tell "work changed"
  // from "the owner wrote to me", which is exactly how a routine tick gets
  // mistaken for an unanswered message. DSH's MessageSourceMap is merge-extensible
  // (each producer declares its own kind, consumers fall through unknown ones).
  const eventMessage = text => createUserMessage({ content: [{ type: 'text', text }], source: { kind: labels.runtimeEvent } });
  const notify = async text => {
    if (disposed) return;
    // A host may hold notify from install() on; it can deliver only once the director
    // session exists (events() is called after that).
    if (!agent) throw new Error('directorHost api: notify is not available before the director session exists (events() is where delivery starts)');
    ctx.agents.withoutInitiator(() => agent.steer(eventMessage(text)));
    await ctx.sessionPersistence.flush();
  };
  // The runtime's own notices go through the outbox (recorded before delivery, replayed
  // after a restart); the host's events are delivered once per id (plugin step 7).
  // A notice is marked delivered only when it was: during shutdown delivery throws, so the
  // notice stays recorded and the next process replays it.
  const deliver = async text => { if (disposed) throw new Error('director stopping: notice kept for replay'); await notify(text); };
  const outbox = await new Outbox({ dir: config.stateDir, deliver, log: line => ctx.logger.info(line) }).load();
  // A slot is free and dispatches were refused: one wake naming them, through the
  // outbox (recorded first; its id is derived from the refusals, so a restart that
  // finds them still on disk re-sends the same id and is not delivered twice). The
  // refusals are dropped only after delivery. Serialized: two ends, one wake.
  let slotWake = Promise.resolve();
  const wakeForSlot = () => {
    slotWake = slotWake.catch(() => {}).then(async () => {
      if (!refusals.size) return;
      const live = children.size(), { threshold, ...limit } = limits();
      if (live >= threshold) return;
      const refused = refusals.entries();
      const id = 'slot-free:' + createHash('sha256').update(refused.map(entry => entry.key).join(',')).digest('hex').slice(0, 40);
      await outbox.send(id, texts.slotFree + JSON.stringify({ live, ...limit,
        refused: refused.map(({ key, ...entry }) => entry) }));
      await refusals.remove(refused);
    });
    slotWake.catch(warn);
    return slotWake;
  };
  // Workers the restart interrupted (plugin step 6, lib/resume.mjs): each resumed once
  // in its own session with a recovery note, at most twice over all restarts; one wake
  // names who was resumed and who was not, and why, through the outbox. A failed one
  // keeps the existing ways on: todo_rebind, batch_redispatch, a fresh dispatch.
  const resumeRecords = new ResumeRecords(config.stateDir);
  const resumeWorkers = async () => {
    if (config.resumeWorkers === false) return;
    const subagents = ctx.get('subagents');
    if (typeof subagents?.sendMessage !== 'function' || typeof subagents.listChildren !== 'function') {
      ctx.logger.warn('resume: no subagents service with sendMessage/listChildren; interrupted workers are not resumed'); return;
    }
    const { plans, settled } = await planWork();
    pruneChildren();
    const candidates = resumeCandidates({ plans, settled, director: config.sessionId,
      isLive: id => children.has(id) || ctx.agents.get(id) !== undefined });
    if (!candidates.length) return;
    const catalog = new Map((await subagents.listChildren(config.sessionId)).map(row => [row.id, row]));
    const resumed = [], failed = [];
    for (const plan of candidates) {
      const child = plan.child_session;
      const row = { attempt_id: plan.attempt_id, child_session: child, ...(plan.node_key ? { node_key: plan.node_key } : {}) };
      let turn;
      try { turn = lastTurn((await ctx.sessionQuery.readSession(child)).events); } catch { turn = undefined; }
      const record = await resumeRecords.stats(child);
      const reason = refusal({ row: catalog.get(child), turn, startedAt, record, maxResumes: 2 });
      // Its own stop (the hand-back covered it) or touched since this process started:
      // not a worker the restart interrupted.
      if (reason === 'not_interrupted' || reason === 'active_since_this_process_started') continue;
      if (reason) { failed.push({ ...row, reason, ...(record ? record : {}) }); continue; }
      const note = recoveryNote(plan);
      // Recorded before the message, so a crash right after it cannot resume a third time.
      await resumeRecords.add(child, { at: new Date().toISOString(), note_sha256: createHash('sha256').update(note).digest('hex') });
      try {
        await subagents.sendMessage(agent, child, [{ type: 'text', text: note }], { signal: AbortSignal.timeout(60000) });
        await resumeRecords.delivered(child).catch(error => ctx.logger.warn('resume record: ' + String(error)));
        resumed.push(row);
      } catch (error) {
        failed.push({ ...row, reason: String(error?.code ?? 'send_failed'), detail: String(error?.message ?? error).slice(0, 300) });
      }
    }
    if (resumed.length || failed.length) await outbox.send('resume:' + startedAt, texts.workersResumed + JSON.stringify({ resumed, failed,
      ...(failed.length ? { failed_next: 'each failed plan is still bound to its dead session: take it over with todo_rebind, or dispatch a fresh worker (batch_redispatch for batch plans)' } : {}) }));
  };
  const notifyEvent = (text, options = {}) => options.id ? outbox.deliverOnce(String(options.id), text).then(() => {}) : notify(text);
  // The api a host gets, complete from install() on (host.d.ts DirectorApi): every
  // member exists; `agent` reads undefined and notify refuses until the session exists.
  const api = { ctx, defineTool, llm, createUserMessage, config, roles, texts, labels, todo, children, jsonOutput,
    liveSessions, planWork, todoPlans, batchRedispatch, notify: notifyEvent, eventMessage, report, currentPhase,
    memory: memory?.store,
    setHostError: async message => { hostError = message; await report(currentPhase()).catch(() => {}); },
    clearHostError: () => { hostError = ''; return report(currentPhase()); },
    isDisposed: () => disposed };
  // A getter, not a copy: the agent exists only after create/resume below.
  Object.defineProperty(api, 'agent', { get: () => agent, enumerable: true });
  // Host-scoped additions of the host first (they precede the plan tools in every
  // request, as they always did), then the worker-facing plan tools.
  await op('install')(api);
  const planRef = { attempt_id: { type: 'string', required: true }, item_id: { type: 'string', required: true } };
  // Worker-facing plan tools are host-scoped: the caller is exec.agent, so a child's
  // todo_begin binds the CHILD session to the plan.
  ctx.tools.register(defineTool({ name: 'todo_status',
    description: texts.tools.todo_status ?? 'Read the frozen todo plan of a work item: brief, goal, next item, done and remaining items. Call it first, and again whenever you lost track (for example after compaction).',
    parameters: { attempt_id: { type: 'string', required: true } }, output: jsonOutput,
    execute: (args, exec) => todo.call(args.attempt_id, ['status', '--attempt=' + args.attempt_id], exec.signal),
  }));
  ctx.tools.register(defineTool({ name: 'todo_begin',
    description: 'Start one plan item before working on it. Refuses unmet needs, captures the state its predicates compare against, and binds the plan to your session. prompt_sha256 (batch sub-plans, optional): the sha256 your prompt named; it is recorded with the binding.',
    parameters: { ...planRef, prompt_sha256: { type: 'string' } }, output: jsonOutput,
    async execute(args, exec) {
      // The CLI refuses, under the plan lock, to take a plan bound to another
      // session. Rebind only when that session is verified dead: not the root
      // and not a live child of this process.
      const begin = ['begin', '--attempt=' + args.attempt_id, '--item=' + args.item_id, '--session=' + exec.agent.session.id,
        ...(args.prompt_sha256 ? ['--prompt-sha256=' + args.prompt_sha256] : [])];
      const result = await todo.call(args.attempt_id, begin, exec.signal);
      if (result.refused !== 'bound_to_other_session') return result;
      const bound = result.bound_session;
      // A plan stays bound only to a session that is still a live child: `has` reads
      // the reconciled set, so a child that ended without its end edge no longer makes
      // a stranded plan look attended.
      if (!bound || bound === config.sessionId || children.has(bound)) return result;
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
  // The host's context source, read for every startup / post-compaction snapshot.
  let contextText = '';
  const contextSource = { refresh: async () => {
    contextText = await host.context(rootRole) + (memory ? await profileSection(memory) : ''); }, render: () => contextText };
  const setup = async (scope, subject) => {
    agentCtx = scope;
    // The host's director-scope pieces come first: its sections, model selection,
    // the snapshot hooks and its own tools, exactly where they always were.
    const injection = new ContextInjection(subject.session, contextSource, createUserMessage,
      { kind: labels.context, section: labels.contextSection, header: texts.contextHeader ?? 'Director context — startup / post-compaction snapshot:\n' });
    // The director prompt section and the snapshot hooks are the director's own; a
    // host that registers sections of its own calls these at the point it wants them
    // (the backend adapter: after its goal and protocol sections and its model selection), and
    // whatever it did not call is installed right after its setup.
    let sectionDone = false, hooksDone = false;
    const runtimeSection = () => { if (sectionDone) return; sectionDone = true;
      scope.systemPrompt.section({ name: labels.sectionPrefix + ':driver-runtime', order: 12, interpolate: false, text: runtimeSkill }); };
    const contextHooks = () => {
      if (hooksDone) return; hooksDone = true;
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
    };
    await op('setup')(scope, subject, { ...api, injection, runtimeSection, contextHooks });
    runtimeSection();
    contextHooks();
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
      description: texts.tools.todo_plan ?? 'Director only. Freeze the todo plan of a work item before dispatching its native child, by expanding a repository template (see todo_templates) with params bound to that template\'s declared params. context_hash is the one your host returned when the work item started. A wrongly typed, missing or unknown param refuses the plan. Items cannot be supplied: when no template fits, add one under todo_templates/ through a reviewed code change.',
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
    scope.tools.register(defineTool({ name: 'todo_batch_plan',
      description: texts.tools.todo_batch_plan ?? 'Director only. Freeze a todo BATCH for a work item from a kind: batch template (todo_templates lists find_verify): one find sub-plan per dimension now, one verify sub-plan per finding once every finder passed. Same arguments as todo_plan. dispatch holds the finder prompts, cut to the batch\'s max_parallel (concurrent finders; the rest are pending), each with its prompt_sha256: give each one verbatim to its own new native subagent, followed by a last line "prompt_sha256: <that sha>". A batch template refuses todo_plan, an ordinary template refuses this tool.',
      parameters: { template: { type: 'string', required: true }, params: { type: 'object', additionalProperties: true, required: true },
        goal: { type: 'string', required: true }, attempt_id: { type: 'string', required: true }, node_key: { type: 'string', required: true },
        context_hash: { type: 'string', required: true }, base_sha: { type: 'string', required: true }, workspace: { type: 'string', required: true } },
      output: jsonOutput,
      execute: (args, exec) => Object.keys(args).some(key => !planArgs.has(key))
        ? { refused: 'unknown_arguments', reason: `todo_batch_plan takes only ${[...planArgs].join(', ')}; sub-plans come from the template`, brief: null }
        : todo.call(args.attempt_id, ['batch-create', '--template=' + args.template, '--params=' + JSON.stringify(args.params),
        '--attempt=' + args.attempt_id, '--node-key=' + args.node_key, '--context-hash=' + args.context_hash,
        '--base-sha=' + args.base_sha, '--workspace=' + args.workspace, '--goal=' + args.goal], exec.signal),
    }));
    scope.tools.register(defineTool({ name: 'todo_batch_status',
      description: 'Director only. Where a todo batch stands and what to dispatch next. Once every finder passed it first expands the verify stage (findings deduplicated and capped; those over the cap are listed unverified, never dropped). dispatch holds prompts cut to the free slots of the stage (find_verify: 4 finders, 4 verifiers at a time), each offered once: give each verbatim to a NEW native subagent, followed by a last line "prompt_sha256: <its sha>" - a session that ever held another sub-plan of this batch is refused. An offered prompt whose child has not begun waits in awaiting_begin; a sub-plan is offered again only when the session bound to it is dead (replaces_session), never while it lives. deadline_passed is a notice only. When complete, run todo_finish on the batch attempt.',
      parameters: { attempt_id: { type: 'string', required: true } }, output: jsonOutput,
      async execute(args, exec) {
        const live = '--live-sessions=' + JSON.stringify(liveSessions());
        const status = await todo.call(args.attempt_id, ['batch-status', '--attempt=' + args.attempt_id, live, '--record-dispatch'], exec.signal);
        if (!status.expand_due) return status;
        return todo.call(args.attempt_id, ['batch-expand', '--attempt=' + args.attempt_id, live, '--record-dispatch'], exec.signal);
      },
    }));
    scope.tools.register(defineTool({ name: 'todo_finish',
      description: texts.tools.todo_finish ?? 'Director only. Re-evaluate every item of an attempt\'s plan; only when all pass, write the git bundle, its sha256 and the candidate report and return their paths for your host\'s report of the work item. Otherwise it is refused with the failing items. For a batch attempt it re-runs nothing: every sub-plan must already have passed, and the candidate also names the fan-in summary (evidence: false - record evidence from each verify report).',
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
    // idle_dormant refuses unfinished_plans for as long as the work is live.
    // (A plan whose work has already settled does not block at all; the takeover
    // is for the stranded plan of work that is still open.)
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
          session: processSession(pid, '/proc', sessionEnv), mySession: config.sessionId, watched: watches.has(pid), count: watches.size });
        if (refusal) return refusal.refused === 'too_many_watches' ? { ...refusal, watching: [...watches.keys()] } : refusal;
        watches.set(pid, { note, since: watches.get(pid)?.since ?? Date.now() });
        if (!watchTimer) { watchTimer = setInterval(pollWatches, watchIntervalMs); watchTimer.unref?.(); }
        return { watching: true, pid, note, check_every_seconds: watchIntervalMs / 1000 };
      },
    }));
    const heldWork = op('heldWork');
    // Questions to the owner (plugin step 8): the host's attention channel. One question
    // per call, under the director's own id, so a retry asks nothing new and the
    // answer comes back naming it. Without a channel both tools refuse, naming why.
    const attention = host.attention;
    const questionId = /^[A-Za-z0-9._:-]{1,64}$/;
    const noChannel = { refused: 'no_question_channel', reason: 'this host has no channel to its owner (no attention capability)' };
    scope.tools.register(defineTool({ name: 'ask_owner',
      description: texts.tools.ask_owner ?? 'Director only. Ask your owner ONE concrete question that only they can decide (a ruling, an authorization, a choice). question_id is yours (1-64 of letters, digits, . _ : -): the same id with the same question is a safe retry and asks nothing new; the same id with another question is refused, so give every question its own id. context is optional background shown with it. The answer comes back to you as an event (whether it names question_id depends on the host: the built-in host names it; a host may instead carry the citation on the answering message). Anything you wait on from your owner must be asked this way. Use close_question when it no longer needs an answer.',
      parameters: { question_id: { type: 'string', required: true }, question: { type: 'string', required: true }, context: { type: 'string' } }, output: jsonOutput,
      async execute(args, exec) {
        if (!attention) return noChannel;
        if (typeof args.question_id !== 'string' || !questionId.test(args.question_id)) return { refused: 'invalid_question_id', reason: 'question_id is 1-64 of letters, digits, . _ : -' };
        const text = typeof args.question === 'string' ? args.question.trim() : '';
        if (!text || text.length > 2000) return { refused: 'invalid_question', reason: 'one question, 1-2000 characters' };
        return attention.ask({ id: args.question_id, text, ...(typeof args.context === 'string' && args.context.trim() ? { context: args.context.trim() } : {}) }, exec.signal);
      },
    }));
    scope.tools.register(defineTool({ name: 'close_question',
      description: texts.tools.close_question ?? 'Director only. Take back one of your own questions that no longer needs an answer (resolved another way, overtaken, asked wrongly), with the reason. It stays visible to your owner as withdrawn, with your reason. A question your owner already answered cannot be closed; closing twice changes nothing.',
      parameters: { question_id: { type: 'string', required: true }, reason: { type: 'string', required: true } }, output: jsonOutput,
      async execute(args, exec) {
        if (!attention) return noChannel;
        const reason = typeof args.reason === 'string' ? args.reason.trim() : '';
        if (typeof args.question_id !== 'string' || !questionId.test(args.question_id)) return { refused: 'invalid_question_id', reason: 'question_id is 1-64 of letters, digits, . _ : -' };
        if (!reason || reason.length > 2000) return { refused: 'invalid_reason', reason: 'say why, 1-2000 characters' };
        return attention.close({ id: args.question_id, reason }, exec.signal);
      },
    }));
    scope.tools.register(defineTool({ name: 'idle_dormant',
      description: texts.tools.idle_dormant ?? `Director only. Call when no authorized work is available and nothing is in flight: stops the 15-minute idle review (no model turns while nothing happens) and compacts a large context once. wake_after_minutes (${DORMANT_MIN_MINUTES}-${DORMANT_MAX_MINUTES}) is how long you choose to sleep: when it runs out with no input you get one review, so pick it from when something you wait on could have changed. You stay subscribed: any host event, owner message or child message wakes you earlier and the review resumes. Before calling it, every decision you wait on from your user must already be an open question. Refused while you have a live child, held work or an unfinished todo plan whose work item has not settled.`,
      parameters: { reason: { type: 'string', required: true }, wake_after_minutes: { type: 'integer', required: true } }, output: jsonOutput,
      async execute(args, exec) {
        const reason = typeof args.reason === 'string' ? args.reason.trim() : '';
        // Not coerced: the JSON string "30" is not an integer number of minutes.
        const wakeAfter = args.wake_after_minutes;
        const badWake = reason && wakeAfterRefusal(wakeAfter);
        if (badWake) return badWake;
        // Reconcile first: a departed child must not refuse dormancy at all (and the
        // readers below reconcile on their own, so an early return cannot skip it).
        pruneChildren();
        const live = children.ids();
        const early = dormancyRefusal({ reason, liveChildren: live.length, childSessions: live });
        if (early) return early;
        let claims, work;
        try {
          const bounded = AbortSignal.any([exec.signal, AbortSignal.timeout(30000)]);
          claims = await heldWork(bounded);
          work = await planWork(bounded);
        } catch (error) {
          return { refused: 'unverifiable', reason: 'could not prove nothing is in flight: ' + String(error.message ?? error).slice(0, 500) };
        }
        // The reads above take time: re-reconcile before reading the plans and refusing.
        pruneChildren();
        const liveAfter = children.ids();
        const plans = unfinishedWithoutChild(work.plans, new Set(liveAfter), work.settled);
        const background = backgroundProcesses(config.sessionId, process.pid, '/proc', sessionEnv).filter(pid => !watches.has(pid));
        const refusal = dormancyRefusal({ reason, liveChildren: liveAfter.length, childSessions: liveAfter,
          claims, plans, background, ...(texts.heldWork ? { heldReason: texts.heldWork } : {}) });
        if (refusal) return refusal;
        if (!idleReview) return { refused: 'starting', reason: 'the runtime is still starting' };
        // Waiting on the owner is a reason to sleep, never a reason to refuse it: the
        // open questions are listed, not counted against dormancy.
        let waitingOnOwner;
        if (attention) {
          try {
            const open = await attention.list(AbortSignal.any([exec.signal, AbortSignal.timeout(15000)]));
            waitingOnOwner = open.map(q => ({ id: q.id, text: q.text }));
            if (open.truncated) waitingOnOwner.push({ truncated: true, note: 'more open questions than the host lists' });
          }
          catch (error) { waitingOnOwner = { unreadable: String(error?.message ?? error).slice(0, 300) }; }
        }
        const dormant = idleReview.enterDormancy(reason, wakeAfter * dormantMinuteMs);
        return { dormant: true, since: dormant.since, until: dormant.until, wake_after_minutes: wakeAfter, reason,
          ...(waitingOnOwner ? { waiting_on_owner: waitingOnOwner } : {}),
          compaction: `once on the next idle if the context is at least ${compactFloor} tokens`,
          wakes_on: texts.wakesOn ?? ['host event', 'owner message', 'child message', 'wake_after_minutes running out'] };
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
    if (memory) registerMemoryTools(scope, { defineTool, jsonOutput, memory,
      ownerMessage: host.memory ? (id, signal) => host.memory.ownerMessage(id, signal) : undefined });
    // Plugin step 12: the steward's read-only view of every project and its handoff to a
    // project's director, when the host provides them (which role sees them is roles.json's).
    if (host.portfolio || host.handoff) registerPortfolioTools(scope, { defineTool, jsonOutput, portfolio: host.portfolio, handoff: host.handoff });
    // Plugin step 13: proposals the owner decides on.
    if (host.proposals) registerProposalTools(scope, { defineTool, jsonOutput, proposals: host.proposals });
  };
  const options = { agentOptions: { provider: config.provider, model: config.model }, setup };
  const resume = await ctx.sessionPersistence.stat(config.sessionId) !== undefined;
  const handle = resume ? await ctx.agents.resume({ ...options, resumeSessionId: config.sessionId })
    : await ctx.agents.create({ ...options, sessionId: config.sessionId, meta: { cwd: process.cwd() } });
  agent = handle.agent;
  // The resumed session's recorded request envelope is NOT rewritten here. Until
  // 2026-11-15 this spot appended a request/header retiring tools the process does
  // not offer; at boot no turn is open, and DSH refuses to load a log with a
  // request/header outside an open turn, so the NEXT restart failed for both
  // drivers (SessionPersistenceCorruptionError, 2026-11-15 00:39Z). DSH writes a
  // fresh envelope inside the first resumed turn itself (reason=resume); a
  // compaction summary before that may replay the old schemas as cache prefix,
  // which asks the provider for nothing.
  idleReview = new IdleReview({ agent, goals: ctx.goals, continuation,
    canContinue: () => !completionError,
    ask: async () => {
      if (disposed) return;
      ctx.agents.withoutInitiator(() => agent.steer(createUserMessage({ content: [{ type: 'text', text: texts.continuationQuestion }], source: { kind: labels.continue } })));
      await ctx.sessionPersistence.flush();
    },
    ...(Number(config.idleReviewMs) > 0 ? { intervalMs: Number(config.idleReviewMs) } : {}),
    compact: async () => {
      const tokens = ctx.tokenMeter.measure(agent.session).totalTokens;
      if (!(tokens >= compactFloor)) return ctx.logger.info(`dormant at ${tokens} tokens: below ${compactFloor}, not compacting`);
      // Native idle compaction; refused (busy) when work arrived meanwhile - then the wake handles it.
      const result = await ctx.compaction.compactNow(agent, AbortSignal.timeout(10 * 60 * 1000));
      ctx.logger.info(`dormant at ${tokens} tokens: ${result ? 'compacted' : 'nothing safe to compact'}`);
    },
    // A dormant phase is reported when it starts or ends, not only at the next status change.
    changed: () => { if (waiting) void report(currentPhase()); },
    notify: async (reason = 'idle_review_due', dormancy) => {
      let todoView;
      try {
        const { plans, settled } = await planWork();
        todoView = { todo_unfinished: unfinishedWithoutChild(plans, children.sessionSet(), settled) };
      }
      catch (error) { ctx.logger.warn(String(error)); todoView = { todo_error: String(error.message ?? error).slice(0, 1000) }; }
      await notify(texts.reviewDue + JSON.stringify({
        ...(config.projectId !== undefined ? { project_id: config.projectId } : {}), reason,
        ...(dormancy ? { dormant_since: dormancy.since, dormant_reason: dormancy.reason } : {}),
        instruction: texts.reviewInstruction,
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
  if (config.permissionPreset) {
    // Resume preserves old pinned policy. Change it through DSH's durable API;
    // children inherit this explicit selection, including forks.
    ctx.get('permissionPresets').set(agent.session, config.permissionPreset);
  }
  ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return;
    // Only real input ends dormancy (a compaction while dormant must not). A kind
    // missing from the wake sources only keeps the timed review parked; the message
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
      return outbox.send(`handback-failed:${session}`, texts.todoHandback + JSON.stringify({ reason: 'todo_handback_failed', child_session: session,
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
      await outbox.send(`handback:${plan.attempt_id}:${session}`, texts.todoHandback + JSON.stringify({ reason: 'todo_unfinished', attempt_id: plan.attempt_id, ...payload }));
    }
    // A batch child that ended may leave its batch waiting on the director: every
    // finder passed (expand and dispatch the verifiers) or everything passed (finish).
    for (const attempt of new Set(plans.filter(p => p.batch && p.child_session === session).map(p => p.batch))) {
      try {
        const { code, result } = await todo.run(['batch-status', '--attempt=' + attempt]);
        if (code === 0 && (result.expand_due || result.complete))
          await outbox.send(`batch:${attempt}:${result.expand_due ? 'expand' : 'complete'}:${session}`, texts.todoBatch + JSON.stringify({ reason: result.expand_due ? 'batch_expand_due' : 'batch_complete',
            attempt_id: attempt, brief: result.brief }));
      } catch (error) { ctx.logger.warn(String(error)); }
    }
  };
  // Observe nested native children too: all activity belongs to this private
  // per-director process, so no other director's worker can enter these counts.
  ctx.on('subagent/start', info => {
    children.start(info);
    void report(currentPhase());
  });
  ctx.on('subagent/end', info => {
    children.end(info); void report(currentPhase());
    void handback(info.id).catch(error => ctx.logger.warn(String(error)));
    wakeForSlot();
  });
  // After a child's successful compaction, its next step gets one line of its
  // bound plan's brief. Host-scoped hooks see native children; the root has
  // its own post-compaction context snapshot instead.
  const compactedChildren = new Set();
  ctx.on('session/event', (session, event) => {
    if (event.type === 'compaction/end' && !event.data.error && session.id !== config.sessionId) compactedChildren.add(session.id);
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
      source: { kind: labels.todoBrief } });
    return { ...decision, messages: [...decision.messages, brief] };
  });
  ctx.on('agent/error', ({ agent: subject, error }) => {
    if (subject !== agent) return;
    completionError = connectionErrorMessage(error, texts.modelCompletion);
    void report('failed');
  });
  let events;
  const close = async () => {
    if (disposed) return;
    disposed = true;
    idleReview.close();
    clearInterval(watchTimer);
    // A host shutdown ends process-local continuation, not the standing goal.
    // Disarm before cancellation so DSH does not persist a user-style pause.
    ctx.goals.disarm(agent);
    events?.stop?.();
    await handle.dispose();
    await ctx.sessionPersistence.flush();
    await activity.catch(() => {});
    await memory?.store?.close().catch(() => {});
  };
  ctx.on('dispose', close);
  await report('running');
  // The host delivers what is already waiting and everything that arrives later
  // (its own subscription, its goal binding); its failure is the director's failure.
  // Notices a previous process recorded but did not deliver (a restart in between).
  // One that fails is kept (or, after repeated boots, set aside) and never stops the boot.
  const { replayed } = await outbox.replay();
  if (replayed.length) ctx.logger.info(`outbox: replayed ${replayed.length} notice(s): ${replayed.join(', ')}`);
  // Dispatches refused before a restart: the restart freed their slots.
  await wakeForSlot();
  // Workers the restart interrupted: resumed before new events arrive. A failure here
  // is logged and never stops the boot.
  await resumeWorkers().catch(error => ctx.logger.warn('resume: ' + String(error?.stack ?? error)));
  events = await host.events(api);
  const pump = Promise.resolve(events?.done).catch(async error => {
    if (!disposed) {
      await report('failed', String(error).slice(0, 2000));
      throw error;
    }
  });
  pump.catch(error => { ctx.logger.error(String(error)); ctx.get('appExit')?.(1); });
  return { host, events, agent, agentCtx, close, pump, api };
}

export function apply(ctx) {
  // Awaiting loader settlement inside apply would deadlock its own startup.
  void boot(ctx).catch(error => { console.error('dsh-director startup:', String(error)); ctx.get('appExit')?.(1); });
}

