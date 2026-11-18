/**
 * directorHost v1: what a work/memory system implements so dsh-director can run on it.
 * Provided in process as the cordis service `directorHost` (ctx.provide). The
 * executable form of this contract is lib/host.mjs `validateHost` (refusals) and
 * conformance/index.mjs (behaviour); this file is their documentation.
 *
 * Built-in implementation: hosts/local.mjs (local files). External: e.g. the
 * the backend adapter in integrations/dsh-driver/runtime.mjs.
 */

/** Work item states, aligned with MCP Tasks / A2A task states. */
export type WorkState = 'working' | 'input_required' | 'completed' | 'failed' | 'cancelled' | 'unknown';

export interface HostConfig {
  /** The director's DSH session id (its native children are workers). */
  sessionId: string;
  provider: string;
  model: string;
  /** Directory for the director's own state (todo plans, finish candidates). */
  stateDir: string;
  /** The DSH entry; DSH libraries are resolved from it. */
  dshEntry: string;
  /** roles.json (director/worker/steward table); role prompts are read next to it. */
  rolesFile: string;
  /** v1.2, plugin step 12: the root session's role, 'director' (default) or 'steward'.
   *  The steward needs a `steward` entry in rolesFile; its native children are workers. */
  rootRole?: 'director' | 'steward';
  /** The todo CLI: `python -m <module>` in `cwd` (default: the process's directory) with
   *  python defaulting to python3; the CLI reads its state directory from `stateDirEnv`. */
  todo: { python?: string; cwd?: string; module: string; stateDirEnv: string };
  /** Environment variable carrying the session id on the driver's processes (default DSH_DIRECTOR_SESSION). */
  sessionEnv?: string;
  /** Included in review notices when set. */
  projectId?: string;
  /** DSH permission preset applied to the director session on start, e.g. 'danger-full-access'. */
  permissionPreset?: string;
  /** The worker capacity this host expects (default: DSH's own maxActiveSubagents). The guard
   *  uses the smaller of this and the limit DSH runs with, so it is never above DSH's. */
  maxActiveSubagents?: number;
  /** Worker slots kept free for resuming workers and follow-ups (default 1, at least 1); the
   *  director's subagent call is refused at capacity - reservedSlots, the refusal names the
   *  dispatch, and one wake naming every refused dispatch comes when a slot frees (kept
   *  across a restart). A dispatch DSH itself refused counts as refused too. */
  reservedSlots?: number;
  idleReviewMs?: number | string;
  dormantMinuteMs?: number | string;
  watchIntervalMs?: number | string;
  dormantCompactMinTokens?: number | string;
  /** v1.2, plugin step 11: the owner's memory (design-steward 3). Absent: no memory and no
   *  memory tools. The repository lives at `<root>/<userId>/` (root default
   *  ~/.local/share/dsh-director/memory; keep it outside anything mounted into containers).
   *  `remote` (off by default) is an Agent Memory Leaderboard endpoint used as the ranked
   *  index instead of the built-in BM25; `amiIds` lets it trace ActiveMemoryIndex hits back to
   *  turns. Every remote call may cost money. */
  memory?: { userId: string; root?: string; grepTimeoutMs?: number; backupRemote?: string;
    remote?: { url: string; name: string; tokenFile?: string; userId?: string; sessionId?: string; amiIds?: boolean } };
}

/** What the director hands the host (host-scoped and director-scoped hooks, events). */
export interface DirectorApi {
  ctx: unknown; defineTool: Function; llm: unknown; createUserMessage: Function;
  config: HostConfig; roles: unknown; texts: Record<string, unknown>; labels: Record<string, string>;
  todo: unknown; children: unknown; jsonOutput: unknown;
  liveSessions(): string[]; planWork(signal?: AbortSignal): Promise<unknown>;
  todoPlans(): Promise<unknown[]>; batchRedispatch(): Promise<unknown[]>;
  /** Steer one wake event into the director (a runtime-event user message). Present from
   *  install() on; it refuses (throws, naming why) until the director session exists.
   *  v1.1: pass `{ id }` - stable across restarts, derived from the event itself, never
   *  from a counter or a random value - and the director delivers each (id, text) once:
   *  the same id with the same text is a redelivery, the same id with other text is a
   *  new event. Checked before delivery, recorded after it: a crash in between
   *  duplicates, never loses. If notify throws, the host must deliver the event again
   *  later under the same id; the conformance suite checks exactly that (a delivery
   *  that failed comes back with the same id), and that every event carries an id. */
  notify(text: string, options?: { id?: string }): Promise<void>;
  eventMessage(text: string): unknown;
  report(phase: string, error?: string, event?: unknown): Promise<void>;
  currentPhase(): string;
  /** A host-side failure the status reports should carry (cleared with clearHostError). */
  setHostError(message: string): Promise<void>;
  clearHostError(): Promise<void>;
  isDisposed(): boolean;
  /** v1.2: the owner's memory store when config.memory is set (lib/memory/store.mjs), else
   *  undefined. A host records each owner/steward turn with memory.recordTurn(turnId,
   *  [{role: 'owner'|'steward', text}]) and runs the owner's own operations (forget,
   *  restore, purge, profile edits) through it - purge exists only here, never as a tool. */
  readonly memory?: unknown;
  /** undefined during install(); the director's agent from setup() on. */
  readonly agent: unknown;
}

export interface DirectorHost {
  /** semver; the director accepts ^1. */
  version: string;
  /** The optional operations this host implements; declared and implemented must match. */
  capabilities: Array<'progress' | 'workState' | 'heldWork' | 'roles' | 'install' | 'setup' | 'texts' | 'labels' | 'attention' | 'memory'
    | 'portfolio' | 'handoff' | 'proposals'>;
  config: HostConfig;

  // ---- required ----
  /** Startup / post-compaction snapshot text for the root role (config.rootRole). */
  context(role: 'director' | 'steward'): Promise<string>;
  /** Deliver what is waiting and everything that arrives later through api.notify, each
   *  event with a stable id (v1.1); deliver before moving your own cursor past it.
   *  `done` rejects on a fatal failure (the director then stops); `stop` ends delivery. */
  events(api: DirectorApi): Promise<{ done?: Promise<unknown>; stop(): void }>;
  // `done` rejects on a fatal failure while running; after stop() it must settle
  // (resolve, or reject with the stop's abort, which the director ignores).

  // ---- optional (absent: the default in brackets) ----
  /** Status report body { phase: running|waiting|dormant|failed, subagents, error, event? } [dropped]. */
  progress?(body: object): Promise<unknown> | void;
  /** A work item's state; settled = completed|failed|cancelled; throw or 'unknown' when unsure [unknown]. */
  workState?(workId: string, signal?: AbortSignal): Promise<WorkState>;
  /** Work this director holds; idle_dormant refuses while any [none]. */
  heldWork?(signal?: AbortSignal): Promise<Array<{ node_key: string }>>;
  /** v1.1, plugin step 8: the director's questions to its owner, behind ask_owner /
   *  close_question [none: both tools refuse, naming that the host has no channel].
   *  - ask: one concrete question under the director's own id. The same id with the
   *    same text asks nothing new (a safe retry); the same id with other text is
   *    refused {refused: 'question_id_reused'}.
   *  - close: take an own, unanswered question back, with the reason, as a record (it
   *    stays visible as withdrawn). An answered one is refused {refused:
   *    'question_answered'}; closing twice changes nothing. What `closed` reports on a
   *    retry depends on the host: a retry the host replays (same request) may answer
   *    {closed: true} again, a retry with another reason {closed: false,
   *    already_closed: true}. Either way the question is closed once; `closed` says "a
   *    withdrawal of this question exists", not "this call withdrew it".
   *  - list: the open questions (a host may bound it; it states its bound, and marks a
   *    truncated answer with `truncated: true` on the returned array).
   *  The owner's answer reaches the director through events(). The built-in host's
   *  event names the question id; another host may carry the citation on the
   *  answering message instead. */
  attention?: {
    ask(question: { id: string; text: string; context?: string }, signal?: AbortSignal): Promise<object>;
    close(close: { id: string; reason: string }, signal?: AbortSignal): Promise<object>;
    list(signal?: AbortSignal): Promise<Array<{ id: string; text: string }>>;
  };
  /** v1.2, plugin step 11: whether `id` names a message the owner wrote; memory_forget
   *  needs it (the steward forgets only when the owner asked) [none: memory_forget refuses]. */
  memory?: { ownerMessage(id: string, signal?: AbortSignal): Promise<boolean> };
  /** v1.2, plugin step 12: the steward's read-only view of every project, behind
   *  portfolio_projects / portfolio_project / portfolio_conversation [none: not registered].
   *  Reads only what the host already lets this identity read; it grants nothing.
   *  - projects: each project's id and title, its director's state, its open questions;
   *  - project: one project's overview, open issues and open questions;
   *  - conversation: one page of the owner/director conversation, newest first, with a
   *    cursor for the next older page. */
  portfolio?: {
    projects(signal?: AbortSignal): Promise<object>;
    /** `page` (v1.2+, optional): the cursors a previous answer gave, passed back as the model gave them. */
    project(projectId: string, signal?: AbortSignal, page?: { openAfter?: string; issuesAfter?: number }): Promise<object>;
    conversation(projectId: string, cursor?: string, signal?: AbortSignal): Promise<object>;
  };
  /** v1.2, plugin step 12: pass the owner's words to a project's director, behind the
   *  handoff tool [none: not registered]. The host checks that citesUserMessageId is a
   *  message the owner wrote (refusing otherwise) and quotes it to the director. */
  handoff?(handoff: { projectId: string; text: string; citesUserMessageId: string }, signal?: AbortSignal): Promise<object>;
  /** v1.2, plugin step 13: the steward's proposals, behind propose / withdraw_proposal
   *  [none: not registered]. propose records an OPEN proposal and nothing else - the owner
   *  approves or declines it through the host, the host executes the approved version and
   *  delivers each result to the steward through events(). kind: create_project |
   *  start_driver | stop_driver; supersedes names the steward's own open proposal it replaces.
   *  withdraw takes back the steward's own open proposal, as a record. */
  proposals?: {
    propose(proposal: { kind: string; payload: object; reason: string; supersedes?: string }, signal?: AbortSignal): Promise<object>;
    withdraw(withdrawal: { id: string; reason: string }, signal?: AbortSignal): Promise<object>;
  };
  /** Extend the loaded role table (e.g. forbid backend writes to workers) [unchanged].
   *  Synchronous; the result is validated (roles.json rules plus per-role
   *  allowed_actions / notes) and a Promise or an invalid table refuses the start. */
  roles?(table: object): object;
  /** Host-scoped tools/sections, before the director's plan tools [none]. */
  install?(api: DirectorApi): Promise<void>;
  /** Director-scope sections/tools/hooks, before the director's own; may call
   *  api.runtimeSection() and api.contextHooks() to place those [none]. */
  setup?(scope: unknown, subject: unknown, api: DirectorApi & { runtimeSection(): void; contextHooks(): void }): Promise<void>;
  /** Wording overrides: continuationQuestion, reviewInstruction, tools.<name>, ... [neutral defaults]. */
  texts?: Record<string, unknown>;
  /** Message source kinds and section names (persisted in session logs) [dsh-director-*]. */
  labels?: Record<string, string>;
}
