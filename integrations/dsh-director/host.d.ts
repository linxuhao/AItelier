/**
 * directorHost v1: what a work/memory system implements so dsh-director can run on it.
 * Provided in process as the cordis service `directorHost` (ctx.provide). The
 * executable form of this contract is lib/host.mjs `validateHost` (refusals) and
 * conformance/index.mjs (behaviour); this file is their documentation.
 *
 * Built-in implementation: hosts/local.mjs (local files). External: e.g. the
 * the State adapter in integrations/dsh-driver/runtime.mjs.
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
  /** roles.json (director/worker table); role prompts are read next to it. */
  rolesFile: string;
  /** The todo CLI: `python -m <module>` in `cwd` (default: the process's directory) with
   *  python defaulting to python3; the CLI reads its state directory from `stateDirEnv`. */
  todo: { python?: string; cwd?: string; module: string; stateDirEnv: string };
  /** Environment variable carrying the session id on the driver's processes (default DSH_DIRECTOR_SESSION). */
  sessionEnv?: string;
  /** Included in review notices when set. */
  projectId?: string;
  /** DSH permission preset applied to the director session on start, e.g. 'danger-full-access'. */
  permissionPreset?: string;
  idleReviewMs?: number | string;
  dormantMinuteMs?: number | string;
  watchIntervalMs?: number | string;
  dormantCompactMinTokens?: number | string;
}

/** What the director hands the host (host-scoped and director-scoped hooks, events). */
export interface DirectorApi {
  ctx: unknown; defineTool: Function; llm: unknown; createUserMessage: Function;
  config: HostConfig; roles: unknown; texts: Record<string, unknown>; labels: Record<string, string>;
  todo: unknown; children: unknown; jsonOutput: unknown;
  liveSessions(): string[]; planWork(signal?: AbortSignal): Promise<unknown>;
  todoPlans(): Promise<unknown[]>; batchRedispatch(): Promise<unknown[]>;
  /** Steer one wake event into the director (a runtime-event user message). */
  notify(text: string): Promise<void>;
  eventMessage(text: string): unknown;
  report(phase: string, error?: string, event?: unknown): Promise<void>;
  currentPhase(): string;
  /** A host-side failure the status reports should carry (cleared with clearHostError). */
  setHostError(message: string): Promise<void>;
  clearHostError(): Promise<void>;
  isDisposed(): boolean;
  readonly agent: unknown;
}

export interface DirectorHost {
  /** semver; the director accepts ^1. */
  version: string;
  /** The optional operations this host implements; declared and implemented must match. */
  capabilities: Array<'progress' | 'workState' | 'heldWork' | 'roles' | 'install' | 'setup' | 'texts' | 'labels'>;
  config: HostConfig;

  // ---- required ----
  /** Startup / post-compaction snapshot text for a role. */
  context(role: 'director'): Promise<string>;
  /** Deliver what is waiting and everything that arrives later through api.notify.
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
