/**
 * AItelier API Client — Fetch API wrapper for all AItelier backend REST endpoints.
 *
 * Ported from web/js/api.js (IIFE → TypeScript ES module).
 * Integrates with:
 *   - authStore       (read-only gating: canWrite)
 *   - connectionStore (network error signalling)
 */

import { get } from 'svelte/store';
import { authStore } from '../stores/auth';
import { setConnected, setDisconnected } from '../stores/connection';

// ── Constants ──────────────────────────────────────────────────────

/** Default request timeout (10 seconds). */
const _DEFAULT_TIMEOUT = 10000;

/** HTTP methods considered safe (no mutation). */
const _SAFE_METHODS: Record<string, boolean> = {
  GET: true,
  HEAD: true,
  OPTIONS: true,
};


// ── ApiError ───────────────────────────────────────────────────────

export class ApiError extends Error {
  public status: number;
  /** Stable machine-readable reason from the server body (api/authz.py), or
   *  null when the response carried none. `status === 0` means the request
   *  never got an answer (network failure / timeout) — that, and only that,
   *  is a connection problem. */
  public code: string | null;
  /** The body's `detail` as the server sent it. A 409 from the steward's
   *  proposal endpoints carries the CURRENT row here, not a sentence. */
  public detail: unknown;

  constructor(status: number, message: string, code: string | null = null, detail: unknown = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.name = 'ApiError';
  }
}


// ── Error → i18n key ───────────────────────────────────────────────

/** Write-denial codes emitted by api/authz.py → translation keys. */
const _DENIAL_KEYS: Record<string, string> = {
  write_denied_not_authenticated: 'error.notAuthenticated',
  write_denied_not_a_writer: 'error.notAWriter',
  write_denied_bad_admin_token: 'error.badAdminToken',
};

/**
 * Translation key describing an error — the single place that decides whether
 * something was a server refusal or a genuine connection failure. Callers do
 * `t(errorMessageKey(err))` instead of inventing their own wording.
 */
export function errorMessageKey(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code && _DENIAL_KEYS[err.code]) return _DENIAL_KEYS[err.code];
    if (err.status === 403) return 'error.forbidden';
    if (err.status === 0) return 'error.network';
    return 'error.requestFailed';
  }
  // A rejected fetch() never reached the server → genuine connection failure.
  if (err instanceof TypeError) return 'error.network';
  return 'error.requestFailed';
}

/**
 * Build an ApiError from a non-OK Response, preserving the server's denial
 * code. Exported because streaming callers (the chat SSE POST) can't go
 * through _request but must classify failures the same way.
 */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  let data: Record<string, unknown> = {};
  try {
    data = (await response.json()) as Record<string, unknown>;
  } catch {
    // Non-JSON error body — fall back to the status line.
  }
  const message =
    (typeof data?.detail === 'string' ? data.detail : '') ||
    (data?.message as string) ||
    response.statusText ||
    'Request failed';
  return new ApiError(response.status, String(message), (data?.code as string) || null, data?.detail ?? null);
}


// ── Core request helper ────────────────────────────────────────────

/**
 * Execute an HTTP request with timeout and error handling.
 *
 * @param method   — HTTP method ("GET", "POST", "PATCH", "DELETE")
 * @param path     — URL path relative to origin (e.g. "/api/projects")
 * @param body     — JSON-serializable request body (POST/PATCH only)
 * @param timeout  — timeout in ms (default 10000)
 */
async function _request<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
  timeout?: number,
): Promise<T> {
  // Read-only guard
  const $auth = get(authStore);
  if (!$auth.canWrite && !_SAFE_METHODS[method]) {
    // Mirror the server's codes so the UI wording is the same whether the
    // refusal came from here or from api/main.py:write_gate.
    throw new ApiError(
      403,
      'Read-only access — sign in as an authorized user to make changes.',
      $auth.email ? 'write_denied_not_a_writer' : 'write_denied_not_authenticated',
    );
  }

  const effectiveTimeout = timeout ?? _DEFAULT_TIMEOUT;
  const url = path; // same-origin in production

  async function _attempt(): Promise<T> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), effectiveTimeout);

    const headers: Record<string, string> = {
      Accept: 'application/json',
    };
    const fetchOptions: RequestInit = {
      method,
      headers,
      signal: controller.signal,
    };

    if (body !== undefined && body !== null && (method === 'POST' || method === 'PATCH' || method === 'PUT')) {
      headers['Content-Type'] = 'application/json';
      fetchOptions.body = JSON.stringify(body);
    }

    try {
      const response = await fetch(url, fetchOptions);
      clearTimeout(timeoutId);
      return _handleResponse<T>(response);
    } catch (err: unknown) {
      clearTimeout(timeoutId);
      return _handleFetchError<T>(err);
    }
  }

  // Idempotent GET retry: on network error, retry once after 1s
  if (method === 'GET') {
    try {
      return await _attempt();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 0) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        try {
          return await _attempt();
        } catch (retryErr: unknown) {
          if (retryErr instanceof ApiError && retryErr.status === 0) {
            setDisconnected();
          }
          throw retryErr;
        }
      }
      throw err;
    }
  }

  return _attempt();
}


function _handleResponse<T>(response: Response): Promise<T> {
  // 204 No Content → null
  if (response.status === 204) {
    return Promise.resolve(null as unknown as T);
  }

  // Successful → parse JSON
  if (response.ok) {
    return response.json() as Promise<T>;
  }

  // Non-OK → parse error detail (and any denial code) from the body
  return errorFromResponse(response).then((err) => {
    throw err;
  });
}


function _handleFetchError<T>(err: unknown): Promise<T> {
  // AbortController timeout
  if (err instanceof Error && err.name === 'AbortError') {
    return Promise.reject(new ApiError(0, 'Request timed out'));
  }

  // Network failure
  if (
    err instanceof TypeError &&
    (err.message === 'Failed to fetch' ||
      err.message.indexOf('fetch') !== -1 ||
      err.message.indexOf('NetworkError') !== -1)
  ) {
    setDisconnected();
    return Promise.reject(new ApiError(0, 'Network error: ' + (err.message || 'failed to fetch')));
  }

  // Any other rejection
  return Promise.reject(
    new ApiError(0, 'Request failed: ' + (err instanceof Error ? err.message : String(err))),
  );
}


// ── Convenience wrappers ───────────────────────────────────────────

function _get<T = unknown>(path: string, timeout?: number): Promise<T> {
  return _request<T>('GET', path, undefined, timeout);
}

function _post<T = unknown>(path: string, body?: unknown, timeout?: number): Promise<T> {
  return _request<T>('POST', path, body, timeout);
}

function _patch<T = unknown>(path: string, body?: unknown): Promise<T> {
  return _request<T>('PATCH', path, body);
}

function _put<T = unknown>(path: string, body?: unknown): Promise<T> {
  return _request<T>('PUT', path, body);
}

function _del<T = unknown>(path: string): Promise<T> {
  return _request<T>('DELETE', path);
}


// ── Public API methods ─────────────────────────────────────────────

// ═════════════════════════════════════════════════════════════════════
//  Projects
// ═════════════════════════════════════════════════════════════════════

export function listProjects(): Promise<Record<string, unknown>[]> {
  return _get('/api/projects');
}

export function getProject(id: string): Promise<Record<string, unknown>> {
  return _get('/api/projects/' + encodeURIComponent(id));
}

export function createProject(body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return _post('/api/projects', body);
}

export function deleteProject(projectId: string): Promise<Record<string, unknown>> {
  return _del('/api/projects/' + encodeURIComponent(projectId));
}

// ═════════════════════════════════════════════════════════════════════
//  Runs
// ═════════════════════════════════════════════════════════════════════

export function listRuns(projectId: string): Promise<Record<string, unknown>> {
  return _get('/api/projects/' + encodeURIComponent(projectId) + '/runs');
}

export function getRun(runId: string): Promise<Record<string, unknown>> {
  return _get('/api/runs/' + encodeURIComponent(runId));
}

export function getTasks(projectId: string): Promise<Record<string, unknown>[]> {
  return _get('/api/projects/' + encodeURIComponent(projectId) + '/tasks');
}

export function getRunDetail(runId: string): Promise<Record<string, unknown>> {
  // GET /api/runs/{id} IS the detail endpoint (steps + cache_stats + manifest).
  // A phantom "/detail" suffix here 404'd every run-detail fetch.
  return _get('/api/runs/' + encodeURIComponent(runId));
}

// ═════════════════════════════════════════════════════════════════════
//  Workspace browsing (pipeline artifacts + project code repo)
// ═════════════════════════════════════════════════════════════════════

export function workspaceTree(
  projectId: string, root: 'dps' | 'code' = 'dps',
): Promise<{ project_id: string; root: string; tree: string[] }> {
  return _get('/api/projects/' + encodeURIComponent(projectId) +
    '/workspace/tree?root=' + encodeURIComponent(root));
}

export function workspaceFile(
  projectId: string, path: string, root: 'dps' | 'code' = 'dps',
): Promise<Record<string, unknown>> {
  return _get('/api/projects/' + encodeURIComponent(projectId) +
    '/workspace/file?path=' + encodeURIComponent(path) +
    '&root=' + encodeURIComponent(root));
}

// Direct URL for the raw bytes of an image in the workspace — fed to <img src>,
// so it is a URL and not a fetch (the browser does the request itself).
export function workspaceRawUrl(
  projectId: string, path: string, root: 'dps' | 'code' = 'dps',
): string {
  return '/api/projects/' + encodeURIComponent(projectId) +
    '/workspace/raw?path=' + encodeURIComponent(path) +
    '&root=' + encodeURIComponent(root);
}

// ═════════════════════════════════════════════════════════════════════
//  Repository (project code repo git panel)
// ═════════════════════════════════════════════════════════════════════

export function repoStatus(projectId: string): Promise<Record<string, unknown>> {
  return _get('/api/projects/' + encodeURIComponent(projectId) + '/repo/status');
}

export function repoArchiveUrl(projectId: string): string {
  return '/api/projects/' + encodeURIComponent(projectId) + '/repo/archive';
}

export function repoCommit(projectId: string, message: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/commit', { message });
}

export function repoPush(projectId: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/push', {});
}

export function repoPull(projectId: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/pull', {});
}

export function repoSync(projectId: string, branch: string): Promise<Record<string, unknown>> {
  // Force sync is destructive server-side; confirm is explicit. branch is
  // required by the backend (RepoSyncBody) — the reset target origin/<branch>.
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/sync',
    { branch, confirm: true, backup: true });
}

export function repoSetRemote(projectId: string, url: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/remote', { url });
}

export function repoMakePR(
  projectId: string,
  opts: { title: string; body?: string; base?: string; head?: string },
): Promise<Record<string, unknown>> {
  // title and head are required by the backend (RepoPRBody / push=true).
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/repo/pr', {
    title: opts.title,
    body: opts.body || '',
    base: opts.base || 'main',
    head: opts.head || null,
    push: true,
  });
}

// ═════════════════════════════════════════════════════════════════════
//  Admin — logged-user tracking
// ═════════════════════════════════════════════════════════════════════

export function getLoggedUsers(limit = 50): Promise<Record<string, unknown>[]> {
  return _get('/api/admin/logged-users?limit=' + encodeURIComponent(String(limit)));
}

export function deleteUser(email: string): Promise<Record<string, unknown>> {
  return _del('/api/admin/logged-users/' + encodeURIComponent(email));
}

// ═════════════════════════════════════════════════════════════════════
//  Trace
// ═════════════════════════════════════════════════════════════════════

export interface TraceQueryOptions {
  category?: string;
  afterSeq?: number;
  order?: 'asc' | 'desc';
  limit?: number;
  /** One step INSTANCE (skillflow_steps.id), not a step_id: a looped step has
   *  one instance per item, and they interleave in the run-wide trace. */
  stepInstanceId?: number;
}

export interface ConnectionCounts {
  total: number;
  authenticated: number;
  anonymous: number;
}

/** Live SSE viewer counts (public shape; writers also get a viewers list). */
export function getConnections(): Promise<ConnectionCounts> {
  return _get('/api/connections');
}

export function getTrace(
  runId: string,
  opts?: TraceQueryOptions,
): Promise<{
  run_id: string;
  traces: Record<string, unknown>[];
  next_seq: number | null;
  has_more: boolean;
  order: string;
}> {
  let path = '/api/runs/' + encodeURIComponent(runId) + '/trace';
  const params: string[] = [];
  if (opts?.category) params.push('category=' + encodeURIComponent(opts.category));
  if (opts?.stepInstanceId != null) params.push('step_instance_id=' + opts.stepInstanceId);
  if (opts?.afterSeq != null) params.push('after_seq=' + opts.afterSeq);
  if (opts?.order) params.push('order=' + opts.order);
  if (opts?.limit != null) params.push('limit=' + opts.limit);
  if (params.length > 0) path += '?' + params.join('&');
  return _get(path);
}

// ═════════════════════════════════════════════════════════════════════
//  All Runs (cross-project)
// ═════════════════════════════════════════════════════════════════════

/**
 * One page of the runs list. The server answers with a WINDOW by default —
 * only the rows inside its own `window_days` — so the response carries the
 * counts a reader needs to know what was left out: `total_count` is every row
 * the server knows, `returned_count` is how many it put in `runs`. Nothing on
 * the page may hardcode either the window or those numbers.
 */
export interface RunsList {
  runs: Record<string, unknown>[];
  window_days?: number | null;
  total_count?: number | null;
  returned_count?: number | null;
}

/**
 * List runs across all projects, optionally filtered by status (e.g. 'running').
 *
 * With `includeAll` the server answers with EVERY row (same keys) instead of
 * the default window. Callers that poll must not pass it: asking once for the
 * archive must not turn a timed poll into a full-archive poll.
 */
export function listAllRuns(status?: string, includeAll?: boolean): Promise<RunsList> {
  const params: string[] = [];
  if (status) params.push('status=' + encodeURIComponent(status));
  if (includeAll) params.push('include_all=true');
  return _get('/api/runs' + (params.length > 0 ? '?' + params.join('&') : ''));
}

/** Catalog of runnable pipelines (built-in and generated): origin, addons, durable state. */
export function listPipelines(): Promise<{ pipelines: Record<string, unknown>[] }> {
  return _get('/api/pipelines');
}

/**
 * One pipeline's COMPOSED graph — steps, edges, and the ids an addon spliced in.
 * Composed, not the source YAML: a base+addon combo has no file of its own.
 */
export function pipelineGraph(config: string): Promise<{
  config_name: string;
  label: string;
  origin: string;
  base: string;
  addons: string[];
  addon_steps: string[];
  begin: string;
  steps: Record<string, unknown>[];
}> {
  return _get('/api/pipelines/' + encodeURIComponent(config) + '/graph');
}

/** Read one durable-state file of a generated pipeline (pipeline_state/<config>/<name>). */
export function pipelineStateFile(
  config: string,
  name: string,
): Promise<{ name: string; content: string; truncated: boolean }> {
  return _get(
    '/api/pipelines/' +
      encodeURIComponent(config) +
      '/state/file?name=' +
      encodeURIComponent(name),
  );
}

// ═════════════════════════════════════════════════════════════════════
//  Checkpoints
// ═════════════════════════════════════════════════════════════════════

/** Approve a checkpoint. Takes NO feedback: skillflow's approve_checkpoint(run_id)
 *  has no feedback parameter, so the server refuses an approval that carries one
 *  (api/meta_routers.py) rather than dropping it. Rejection is the channel. */
export function approveCheckpoint(projectId: string, observed: Record<string, unknown>): Promise<void> {
  return _post('/api/meta/' + encodeURIComponent(projectId) + '/checkpoint/approve', {
    checkpoint: observed.checkpoint,
    project_id: projectId,
    run_id: observed.run_id,
    expected_checkpoint: observed.expected_checkpoint,
    decision_id: observed.decision_id,
    rescue_failed: observed.failed_rescue === true,
  });
}

export function rejectCheckpoint(projectId: string, feedback: string, observed: Record<string, unknown>): Promise<void> {
  return _post('/api/meta/' + encodeURIComponent(projectId) + '/checkpoint/reject', {
    checkpoint: observed.checkpoint,
    run_id: observed.run_id,
    project_id: projectId,
    feedback: feedback || '',
  });
}

// ═════════════════════════════════════════════════════════════════════
//  Identity / write permission
// ═════════════════════════════════════════════════════════════════════

export function whoami(): Promise<{
  email: string;
  can_write: boolean;
  gate_enabled: boolean;
  /** Where this deployment sends a reader to sign in. Empty = nowhere to go. */
  signin_url?: string;
  /** 'credential_rejected' when a credential was presented and refused. */
  auth_error?: string | null;
}> {
  return _get('/api/me');
}

/** Client-side write permission toggle (no network call).
 *
 *  `update`, not `set`: a whole-object set has to restate every field, and the
 *  ones it forgot were silently wiped — `lang` already, and a forgotten
 *  `signinUrl` would make the sign-in button disappear the first time write
 *  permission was toggled. */
export function setCanWrite(value: boolean): void {
  authStore.update((prev) => ({ ...prev, canWrite: value, permissionResolved: true }));
}

// ═════════════════════════════════════════════════════════════════════
//  User language preference
// ═════════════════════════════════════════════════════════════════════

export function getUserLang(): Promise<{ lang: string | null }> {
  return _get('/api/settings/user/language');
}

/** Set user language — uses raw fetch (NOT _post) because per-user
 *  preferences must work for ALL users, including read-only. */
export async function setUserLang(lang: string): Promise<{ lang: string | null }> {
  const res = await fetch('/api/settings/user/language', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ lang }),
  });
  if (!res.ok) {
    const err: Record<string, unknown> = await res.json().catch(() => ({}));
    throw new ApiError(res.status, (err.detail as string) || res.statusText || 'Failed to set language');
  }
  return res.json();
}

// ═════════════════════════════════════════════════════════════════════
//  Project actions (write-gated)
// ═════════════════════════════════════════════════════════════════════

/**
 * Retry a failed project.
 * POST /api/projects/{projectId}/retry
 */
export function retryProject(projectId: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/retry');
}

/**
 * Update project fields (name, brief, priority, status).
 * PATCH /api/projects/{projectId}
 */
export function patchProject(
  projectId: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return _patch('/api/projects/' + encodeURIComponent(projectId), body);
}

// ═════════════════════════════════════════════════════════════════════
//  Checkpoints (meta router)
// ═════════════════════════════════════════════════════════════════════

/**
 * Get the current pending checkpoint for a project, if any.
 * GET /api/meta/{projectId}/checkpoint
 */
export function getCheckpoint(
  projectId: string,
): Promise<Record<string, unknown> | null> {
  return _get('/api/meta/' + encodeURIComponent(projectId) + '/checkpoint');
}

// ═════════════════════════════════════════════════════════════════════
//  Repositories
// ═════════════════════════════════════════════════════════════════════

/** Summary of a project belonging to a repository group. */
export interface RepoProjectSummary {
  project_id: string;
  name: string;
  status: string;
  updated_at: string;
  /** Cache stats (hit ratio, token counts) — enriched by the backend via
   *  enrich_project_status + compute_cache_stats_batch, same as Dashboard. */
  cache_stats?: Record<string, unknown>;
  /** Config name (e.g. dpe_default_v2, or an addon alias like dpe_game),
   *  added by enrich_project_status. */
  config_name?: string;
  /** Base pipeline of config_name (a composed alias decomposes to base + addons). */
  config_base?: string;
  /** Composing addon names (e.g. ["game_harness"]); empty for a plain base. */
  config_addons?: string[];
}

/** A repository group, aggregating projects sharing the same repo_path. */
export interface RepoItem {
  repo_path: string;
  repo_name: string;
  repo_type: string;
  repo_url: string | null;
  project_count: number;
  representative_project_id: string;
  last_activity: string;
  projects: RepoProjectSummary[];
}

/** Single repository detail (same shape as RepoItem). */
export type RepoDetail = RepoItem;

/** List all repositories grouped by repo_path. GET /api/repos */
export function listRepos(): Promise<RepoItem[]> {
  return _get('/api/repos');
}

/** Get a single repository detail by its repo_path. GET /api/repos/{repo_path} */
export function getRepo(repoPath: string): Promise<RepoDetail> {
  return _get('/api/repos/' + encodeURIComponent(repoPath));
}


// Since the owner's ruling of 2026-09-22 an OPENED project's graph and working
// notes read publicly ("public的 project的working note也可以public，private
// project的working note继续private"), replacing the old claim here that "State
// project data is private ... require writer authorization". Everything else stays
// writer-only, so do not merge a private project's data into public repo payloads.
export function runWorkflowGraph(runId: string): Promise<Record<string, any>> {
  return _get('/api/runs/' + encodeURIComponent(runId) + '/graph');
}
/** The paged project catalog. `includeArchived` is the OWNER asking for the rows
 *  the archive hid: the request then carries `include_archived=true` and every
 *  returned row carries its `archived` fact. Asking for nothing keeps the request
 *  byte-identical to the one this client always made. */
export function stateProjects(repoPath?: string, after = '', includeArchived = false): Promise<{projects: import('./stateGraph').StateProjectRow[]; next_after: string | null}> {
  const query = new URLSearchParams({limit: '100', after});
  if (repoPath) query.set('repo_path', repoPath);
  if (includeArchived) query.set('include_archived', 'true');
  return _get('/api/state/projects?' + query.toString());
}
/** Hide (`archived`) or restore one project's listing entry: the State write the
 *  projects page's archive/restore control issues. The server owns the door - a
 *  credential that may not archive is REFUSED with its own message, surfaced to
 *  the caller instead of being swallowed. */
export function stateProjectArchive(projectId: string, archived: boolean, reason?: string): Promise<unknown> {
  const args: Record<string, unknown> = {project_id: projectId};
  if (reason) args.reason = reason;
  return _post('/api/state/commands/' + (archived ? 'archive_project' : 'unarchive_project'), args);
}
export interface DriverActivity {
  total: number; online: number; running_subagents: number; running_pipelines: number;
  /** `role` is "steward" for the owner's steward (the driver of `aitelier-home`)
   *  and "director" for every other project; a row without it is a director. */
  managed?: {driver_id: string; enabled: number; phase: string; last_error: string; model: string; role?: 'steward' | 'director'};
}
export interface DriverStatusResponse {
  total: number; online: number; projects: Record<string, DriverActivity>;
}
export function driverStatus(): Promise<DriverStatusResponse> { return _get('/api/dsh-drivers'); }
export interface DriverHistoryEntry {seq: number; type: string; name: string | null; timestamp: number | null; text: string; truncated: boolean}
export interface DriverHistory {
  driver_id: string; session_id: string;
  entries: DriverHistoryEntry[];
  has_more: boolean; next_before_seq: number | null;
}
export function dshDriverHistory(projectId: string, beforeSeq?: number): Promise<DriverHistory> {
  return _get('/api/dsh-drivers/' + encodeURIComponent(projectId) + '/history' + (beforeSeq === undefined ? '' : '?before_seq=' + beforeSeq));
}
export function messageDshDriver(projectId: string, driverId: string, body: string): Promise<unknown> {
  return _post('/api/state/commands/send_driver_message', {
    request_key: crypto.randomUUID(), target_driver_id: driverId, project_id: projectId,
    subject: 'Dashboard message', body, kind: 'note', delivery_mode: 'transient',
  });
}
export interface DriverUserMessage {
  message_id: string; sender_driver_id: string; project_id: string;
  subject: string; body: string; refs: Record<string, string>; created_at: string;
}
export interface DriverUserMessages {
  project_id: string; messages: DriverUserMessage[]; matched_total: number; truncated: boolean;
}
/** Durable messages the project's driver sent its user, newest first. */
export function dshDriverMessages(projectId: string, limit = 50): Promise<DriverUserMessages> {
  return _get('/api/dsh-drivers/' + encodeURIComponent(projectId) + '/messages?limit=' + limit);
}
/** One tagged block. `id` is set on every block written after segment ids
 *  existed; a block written before that has none, and a reply cannot cite it. */
export interface DriverConversationSegment {
  id?: string; kind: 'info' | 'question'; text: string;
}
/** One question a reply answers: the driver message and the block in it. */
export interface DriverConversationAnswerRef {
  message_id: string; segment_id: string;
}
export interface DriverConversationEntry {
  message_id: string; thread_id: string; project_id: string;
  sender_kind: 'driver' | 'user'; sender_driver_id: string | null;
  subject: string; body: string; refs: Record<string, string>;
  reply_to_message_id: string | null; created_at: string;
  /** Tagged blocks, when the sender wrote any. A `question` block is one the
   *  reader owes an answer to; it is presentation, never a checkpoint. */
  segments?: DriverConversationSegment[] | null;
  /** The questions this reply answers. Absent (not empty) when it answers none. */
  answers?: DriverConversationAnswerRef[];
  /** The sender's own questions this row withdraws (withdraw_user_question),
   *  each with its reason. Absent when it withdraws none. */
  withdraws?: { message_id: string; segment_id: string; reason: string }[];
  /** Step 24.2b: the files this message carries. Absent (not empty) when it has none. */
  attachments?: DriverConversationAttachment[];
}
/** One attachment of a conversation message (design 7.8.12a). `id` is a dashboard
 *  upload's `u_` upload_id or a library `m_` ref; the bytes are at `mediaUrl`. A refused
 *  one (the host's library did not take it, or nobody came for it) carries `refused` and
 *  may have no id. A channel message's attachment may lack mime and size. */
export interface DriverConversationAttachment {
  id?: string; kind: 'image' | 'voice' | 'file' | string; mime?: string; size?: number; name?: string;
  refused?: string;
}
export interface DriverConversation {
  project_id: string; messages: DriverConversationEntry[]; has_more: boolean;
  next_cursor: string | null; end_of_history: boolean; matched_total: number;
  unread_count: number; read_upto_message_id: string | null; read_upto_created_at: string | null;
}
/** ONE conversation, both directions, newest first. `cursor` is `next_cursor`
 *  from the previous page: a keyset on (created_at, message_id), so walking
 *  every page returns each message exactly once; `next_cursor: null` is the
 *  end of history. */
export function dshDriverConversation(projectId: string, cursor?: string | null, limit = 30): Promise<DriverConversation> {
  const query = new URLSearchParams({limit: String(limit)});
  if (cursor) query.set('cursor', cursor);
  return _get('/api/dsh-drivers/' + encodeURIComponent(projectId) + '/conversation?' + query.toString());
}
export interface DriverConversationUnread {
  /** Unread driver messages per project, newer than the persisted read marker. */
  projects: Record<string, number>; total: number;
  /** Open questions per project: `question` segments no reply's `answers` cites.
   *  This is the questions box's own rule (`openQuestions` in questions.ts), so
   *  the badge and the box cannot disagree. A project with none is ABSENT. */
  questions: Record<string, number>;
  /** The sum of `questions`: 0 when no project has an open question. */
  questions_total: number;
}
/** The persisted badges per project: driver messages newer than the stored read
 *  marker, plus the count of open questions. Needs no driver online and reads no
 *  message body. */
export function dshDriverConversationUnread(projectId?: string): Promise<DriverConversationUnread> {
  return _get('/api/dsh-drivers/unread' + (projectId ? '?project_id=' + encodeURIComponent(projectId) : ''));
}
/** The user's half of the conversation: one durable row plus the driver's inbox
 *  delivery. `replyToMessageId` is stored on the row; the driver may be offline. */
export function sendDriverConversationMessage(projectId: string, body: string,
  options: {replyToMessageId?: string | null; targetDriverId?: string | null;
    answers?: DriverConversationAnswerRef[] | null; attachments?: string[] | null} = {}): Promise<unknown> {
  const args: Record<string, unknown> = {request_key: crypto.randomUUID(), project_id: projectId, body};
  if (options.replyToMessageId) args.reply_to_message_id = options.replyToMessageId;
  if (options.targetDriverId) args.target_driver_id = options.targetDriverId;
  // Sent only when there is a question to close, so an ordinary reply is the
  // same request it always was.
  if (options.answers?.length) args.answers = options.answers;
  // Likewise the upload_ids: only when the message carries files.
  if (options.attachments?.length) args.attachments = options.attachments;
  return _post('/api/state/commands/send_user_conversation_message', args);
}
/** The same-origin route that serves one attachment's bytes to this project's readers
 *  (a `u_` upload or an `m_` ref named by the conversation). */
export function mediaUrl(projectId: string, id: string): string {
  return '/api/dsh-drivers/' + encodeURIComponent(projectId) + '/conversation/media/' + encodeURIComponent(id);
}
export interface ConversationUpload {
  upload_id: string; kind: string; mime: string; size: number; name: string;
}
/** How long one upload may take: a phone on a slow line sending 25 MiB needs far more
 *  than `_request`'s 10 seconds. */
export const UPLOAD_TIMEOUT_MS = 120000;
/** Step 24.2b: stage one file for the conversation (design 7.8.12a "上传"). The body is
 *  the raw bytes, not multipart; the server decides the kind from the first bytes, so the
 *  name is only the name shown. It is an XMLHttpRequest rather than `_request`: it needs
 *  its own 120 s timeout and upload progress, which fetch does not report. A refusal is an
 *  ApiError whose message is the server's code (`too_large`, `type`, `rate_limited`,
 *  `staging_full`, ...). */
export function uploadConversationFile(projectId: string, file: Blob, name: string,
  // eslint-disable-next-line no-unused-vars -- a parameter name inside a function type
  onProgress?: (fraction: number) => void): Promise<ConversationUpload> {
  const $auth = get(authStore);
  if (!$auth.canWrite) {
    return Promise.reject(new ApiError(403, 'Read-only access — sign in as an authorized user to make changes.',
      $auth.email ? 'write_denied_not_a_writer' : 'write_denied_not_authenticated'));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/dsh-drivers/' + encodeURIComponent(projectId) + '/conversation/uploads?name='
      + encodeURIComponent(name));
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('X-AItelier-Upload', '1');
    if (file.type) xhr.setRequestHeader('Content-Type', file.type);
    if (onProgress) xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    xhr.onload = () => {
      let data: Record<string, unknown> = {};
      try { data = JSON.parse(xhr.responseText || '{}') as Record<string, unknown>; } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(data as unknown as ConversationUpload); return; }
      const detail = typeof data.detail === 'string' ? data.detail : '';
      reject(new ApiError(xhr.status, detail || xhr.statusText || 'Request failed', (data.code as string) || null, data.detail ?? null));
    };
    xhr.ontimeout = () => reject(new ApiError(0, 'Request timed out'));
    xhr.onerror = () => reject(new ApiError(0, 'Network error'));
    xhr.send(file);
  });
}
/** Advance the persisted read marker to a message the user was actually shown. */
export function markDriverConversationRead(projectId: string, messageId: string): Promise<unknown> {
  return _post('/api/state/commands/mark_user_conversation_read', {project_id: projectId, message_id: messageId});
}
/** Create a State project with the signed-in owner's session (plugin step 10).
 *  It is NOT published: opening it stays the separate open_project decision. */
export function createStateProject(projectId: string, title: string, sourceProjectId?: string): Promise<unknown> {
  const args: Record<string, unknown> = {project_id: projectId, title};
  if (sourceProjectId) args.source_project_id = sourceProjectId;
  return _post('/api/state/commands/create_project', args);
}
export function startDshDriver(projectId: string): Promise<unknown> {
  return _post('/api/dsh-drivers/' + encodeURIComponent(projectId) + '/start', {});
}
export function stopDshDriver(projectId: string): Promise<unknown> {
  return _post('/api/dsh-drivers/' + encodeURIComponent(projectId) + '/stop');
}
export function stateOverview(projectId: string): Promise<import('./stateGraph').StateOverview> {
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/overview');
}
export function stateNode(projectId: string, node: string): Promise<import('./stateGraph').StateNodeDetail> {
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/nodes/' + encodeURIComponent(node));
}
export function stateIssues(projectId: string, open: boolean, after = 0): Promise<{issues: import('./stateGraph').StateIssueSummary[]; status_counts: Record<string, number>; has_more: boolean; next_after: number}> {
  const query = new URLSearchParams({after: String(after), limit: '50'});
  if (open) query.set('status', 'open');
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/issues?' + query.toString());
}
export function stateIssue(projectId: string, issueId: string): Promise<import('./stateGraph').StateIssue> {
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/issues/' + encodeURIComponent(issueId));
}
export function stateAttempts(projectId: string, after = 0): Promise<{attempts: import('./stateGraph').StateAttempt[]; next_after: number | null}> {
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/attempts?after=' + after + '&limit=30');
}
/** The driver's working notes. Public for a project that has been opened
 *  (owner's ruling of 2026-09-22: let's open up the working note for public
 *  projects too). For a project nobody opened the server answers 403, and that
 *  refusal is the server's, not this client's. */
export type DriverNote = { project_id: string; revision: number; index: { address: string; index_line: string }[];
  entry_count: number; listed_count: number; delisted_count: number };
export function stateDriverNote(projectId: string): Promise<DriverNote> {
  return _get('/api/state/projects/' + encodeURIComponent(projectId) + '/driver-note');
}
export function stateAttemptDetail(attemptId: string): Promise<import('./stateGraph').AttemptDetail> {
  return _get('/api/state/attempts/' + encodeURIComponent(attemptId) + '/detail');
}
export function stateRunOwners(runId: string): Promise<{links: import('./stateGraph').RunOwner[]; run_id: string}> {
  return _get('/api/state/runs/' + encodeURIComponent(runId) + '/owners');
}
export function stateRefreshProject(projectId: string, after = 0): Promise<{results: {attempt_id: string; status?: string; error?: string}[]; next_after: number | null; effects: string}> {
  // Explicit user action. No automatic launch/checkpoint/acceptance on reads.
  return _post('/api/state/commands/refresh_project', {project_id: projectId, after, limit: 10}, 60000);
}


/** Exact run history, not the legacy /api/runs execution-project summaries. */
export function runHistory(options: {q?:string;status?:string;workflow?:string;state_project_id?:string;offset?:number;limit?:number} = {}): Promise<{runs:import('./navigation.svelte').RunRow[];total:number;next_offset:number|null}> {
  const query=new URLSearchParams();
  for(const [key,value] of Object.entries(options)) if(value!==undefined && value!=='')query.set(key,String(value));
  return _get('/api/run-history?'+query.toString());
}


export function stateRunSummary(projectId:string):Promise<import('./stateGraph').ProjectRunSummary> {
  return _get(`/api/state/projects/${encodeURIComponent(projectId)}/run-summary`);
}


// ═════════════════════════════════════════════════════════════════════
//  The owner's steward (plugin step 14): proposals and memory requests
// ═════════════════════════════════════════════════════════════════════

export type StewardProposalStatus = 'open' | 'executing' | 'done' | 'failed' | 'declined' | 'withdrawn' | 'superseded';
export interface StewardProposal {
  proposal_id: string; version: number;
  kind: 'create_project' | 'start_driver' | 'stop_driver' | 'archive_project' | 'unarchive_project' | 'connection_change';
  payload: Record<string, any>; reason: string; status: StewardProposalStatus;
  created_at: string; decided_at: string | null; decided_by: string | null; result: unknown;
}
/** What stopping a driver would interrupt. */
export interface StewardImpact {
  running_subagents: {session_id: string; [key: string]: unknown}[];
  unsettled_attempts: {attempt_id: string; [key: string]: unknown}[];
}
export function listStewardProposals(status: 'open' | 'all' = 'open', limit = 50): Promise<StewardProposal[]> {
  return _post<{proposals?: StewardProposal[]; result?: {proposals?: StewardProposal[]}}>(
    '/api/state/query/list_steward_proposals', {status, limit})
    .then(r => (r.result ?? r).proposals ?? []);
}
/** Approve exactly the version the owner is looking at; 409 carries the current row in `detail`. */
export function approveStewardProposal(id: string, expectedVersion: number, confirmInterrupt = false): Promise<StewardProposal> {
  const body: Record<string, unknown> = {expected_version: expectedVersion};
  if (confirmInterrupt) body.confirm_interrupt = true;
  return _post('/api/steward/proposals/' + encodeURIComponent(id) + '/approve', body);
}
export function declineStewardProposal(id: string, expectedVersion: number, reason?: string): Promise<StewardProposal> {
  const body: Record<string, unknown> = {expected_version: expectedVersion};
  if (reason) body.reason = reason;
  return _post('/api/steward/proposals/' + encodeURIComponent(id) + '/decline', body);
}
export function stewardProposalImpact(id: string): Promise<StewardImpact> {
  return _get('/api/steward/proposals/' + encodeURIComponent(id) + '/impact');
}
export type StewardMemoryKind = 'profile_read' | 'profile_update' | 'forgotten_list' | 'forget' | 'restore' | 'purge';
export interface StewardMemoryRequest {
  request_id: string; kind?: StewardMemoryKind; status: 'pending' | 'done'; result?: Record<string, any>;
}
/** The owner's memory operations run in the steward's host, so each one is a request the
 *  steward completes; poll `stewardMemoryRequest` for its result. */
export function createStewardMemoryRequest(kind: StewardMemoryKind, payload: Record<string, unknown> = {}): Promise<StewardMemoryRequest> {
  return _post('/api/steward/memory-requests', {kind, payload});
}
export function stewardMemoryRequest(requestId: string): Promise<StewardMemoryRequest> {
  return _get('/api/steward/memory-requests/' + encodeURIComponent(requestId));
}

// ── Scheduled reminders (plugin step 16). The backend computes every run time;
//    409 carries {code: 'version_conflict', schedule: current row} in `detail`,
//    422 carries {code, reason}.
export type StewardScheduleSpec = {kind: 'once'; at: string} | {kind: 'cron'; expr: string};
export interface StewardSchedule {
  schedule_id: string; version: number; title: string; instruction: string;
  schedule: StewardScheduleSpec; tz: string; enabled: boolean | number;
  next_run_at: string | null; last_run_at: string | null; last_missed: number;
  created_by: 'owner' | 'steward'; updated_by: 'owner' | 'steward'; last_fired_by: string | null;
  created_at: string; updated_at: string;
  /** Seconds an enabled row is past its next run (backend); > OVERDUE_AFTER_S means its clock stopped. */
  overdue_seconds?: number;
  /** Step 25 C11: 'family_group' posts the instruction, as written, into the family group bound
   *  when the reminder was made (no steward turn); `target_unbound` once that group is unbound. */
  target?: 'owner' | 'family_group'; target_channel?: OwnerChannelName | null; target_group_ref?: string | null;
  target_unbound?: boolean;
  /** Step 23.1: the target group's name; 'group' for a one-time reminder a family member made in
   *  that group (the dashboard shows who: created_by_label). */
  target_group_label?: string | null; created_via?: 'group' | null; created_by_label?: string | null;
}
export interface StewardScheduleFields {
  title?: string; instruction?: string; schedule?: StewardScheduleSpec; tz?: string; enabled?: boolean;
  /** Step 23.1: group_ref names the group (required when several are bound). */
  target?: 'owner' | 'family_group'; channel?: OwnerChannelName; group_ref?: string;
}
export function listStewardSchedules(): Promise<StewardSchedule[]> {
  return _get<{schedules?: StewardSchedule[]}>('/api/steward/schedules').then(r => r.schedules ?? []);
}
export function createStewardSchedule(fields: StewardScheduleFields): Promise<StewardSchedule> {
  return _post('/api/steward/schedules', fields);
}
export function updateStewardSchedule(id: string, expectedVersion: number, fields: StewardScheduleFields): Promise<StewardSchedule> {
  return _put('/api/steward/schedules/' + encodeURIComponent(id), {expected_version: expectedVersion, ...fields});
}
export function deleteStewardSchedule(id: string, expectedVersion: number): Promise<{deleted: boolean}> {
  return _del('/api/steward/schedules/' + encodeURIComponent(id) + '?expected_version=' + expectedVersion);
}
/** The next 5 runs as the backend computes them (ISO strings with the zone's offset). */
export function previewStewardSchedule(schedule: StewardScheduleSpec, tz: string): Promise<string[]> {
  return _post<{next_runs?: string[]}>('/api/steward/schedules/preview', {schedule, tz}).then(r => r.next_runs ?? []);
}

// ── Owner chat channels (plugin step 19, design 7.2). Owner session only: the
//    plaintext bind code exists in exactly one response, the POST below.
export type OwnerChannelName = 'slack' | 'wechat';
/** One row of the channel-down alert ledger (design 7.9.2): one per outage and kind. */
export interface OwnerChannelAlertRow {
  kind: 'down' | 'up'; down_since: string; state: 'claimed' | 'sent' | 'failed'; attempt: number;
  via: OwnerChannelName; claimed_at: string; finished_at: string | null; error: string | null;
}
export interface OwnerChannel {
  channel: OwnerChannelName; bound: boolean; sender_ref: string | null;
  bound_at: string | null; revoked_at: string | null; is_default: boolean | number;
  /** What the steward's host last reported for this channel (and when); null when it reported nothing.
   *  Design 7.9.2: the facts (live: the last reading was a real one; down_since: this outage's
   *  start; up_since: the last real reconnect) and the channel-down alert ledger's projections. */
  status?: {connected: boolean; detail?: string | null; at?: string; live?: boolean; down_since?: string | null;
    up_since?: string | null; alert_down?: OwnerChannelAlertRow | null; alert_up?: OwnerChannelAlertRow | null;
    last_told?: {kind: 'down' | 'up'; down_since: string; at: string} | null; last_down_sent_at?: string | null;
    alert_due?: {kind: 'down' | 'up'; attempt: number} | null} | null;
  /** While a WeChat login needs a scan: its QR as an SVG document (the backend checked it). */
  qr_svg?: string | null;
  /** The newest request for a new login QR on this channel (WeChat only), or null. */
  login_request?: OwnerChannelLoginRequest | null;
}
/** The owner's request for a new WeChat login QR: `pending` until the steward's host runs it,
 *  `expired` when no host did within 2 minutes, `done` with the bridge's answer. Never the QR. */
export interface OwnerChannelLoginRequest {
  request_id: string; channel: OwnerChannelName; status: 'pending' | 'done' | 'expired';
  requested_at: string; completed_at: string | null;
  result: {requested?: true; refused?: string; retry_after_s?: number} | null;
}
/** The backend's own rules of the channel-down alert (core/state_channels.py alert_limits). */
export interface OwnerChannelAlertLimits {
  after_minutes: number; retry_after_minutes: number; unreported_minutes: number; attempts: number;
  down_every_minutes: number; quiet_from_hour: number; quiet_until_hour: number;
}
export interface OwnerChannels { channels: OwnerChannel[]; default_channel: OwnerChannelName | null;
  /** Design 7.9.2: the server's clock, the owner's quiet hours now (until their next end) and zone,
   *  and the alert's rules the card's times are computed with. */
  now?: string; quiet_now?: boolean; quiet_until?: string | null; owner_tz?: string;
  alert_limits?: OwnerChannelAlertLimits }
export function listOwnerChannels(): Promise<OwnerChannels> {
  return _get('/api/steward/channels');
}
export function createOwnerChannelCode(channel: OwnerChannelName): Promise<{code: string; expires_at: string}> {
  return _post('/api/steward/channels/' + encodeURIComponent(channel) + '/code');
}
/** Ask for a new WeChat login QR; a request still pending is answered again, not doubled. */
export function requestOwnerChannelQr(channel: OwnerChannelName): Promise<OwnerChannelLoginRequest> {
  return _post('/api/steward/channels/' + encodeURIComponent(channel) + '/qr');
}
export function unbindOwnerChannel(channel: OwnerChannelName): Promise<unknown> {
  return _del('/api/steward/channels/' + encodeURIComponent(channel));
}
export function setDefaultOwnerChannel(channel: OwnerChannelName | null): Promise<unknown> {
  return _put('/api/steward/channels/default', {channel});
}
// ── The family groups (step 23, design 7.7; step 23.1, 7.7.24: up to groups_per_channel groups
//    a channel, each named apart). Owner session only. Every per-group call names its group_ref
//    (URL-encoded); the code is issued per channel, for a named group.
export interface GroupMember { sender_ref: string; label: string; named: boolean }
export interface GroupRecord {
  message_id: string; direction: 'in' | 'out'; kind: string; accepted: number; sender_ref: string | null;
  sender_role: 'owner' | 'member' | null; sender_label: string | null; body: string | null; original: string | null;
  withheld_reason: string | null; reply_to: string | null; answered_by: string | null; at: string;
}
export interface FamilyMemoryEntry { entry_id: string; text: string; at: string; added_by_role: string | null; added_by_label: string | null }
export interface OwnerGroup {
  channel: OwnerChannelName; group_ref: string; label: string | null; mention_prefix: string; bound_at: string;
  messages_30d: number; last_message_at: string | null; members: GroupMember[];
  messages?: GroupRecord[]; family_note?: {text: string; updated_at: string} | null; family_memory?: FamilyMemoryEntry[];
}
const groupPath = (channel: OwnerChannelName, groupRef: string) =>
  '/api/steward/groups/' + encodeURIComponent(channel) + '/' + encodeURIComponent(groupRef);
/** Every bound group; with channel and groupRef, that one group's records, note and memory too. */
export function listOwnerGroups(channel?: OwnerChannelName, groupRef?: string): Promise<{groups: OwnerGroup[]; groups_per_channel: number}> {
  return _get('/api/steward/groups' + (channel && groupRef
    ? '?channel=' + encodeURIComponent(channel) + '&group_ref=' + encodeURIComponent(groupRef) : ''));
}
export function createGroupCode(channel: OwnerChannelName, label: string): Promise<{code: string; expires_at: string}> {
  return _post('/api/steward/groups/' + encodeURIComponent(channel) + '/code', {label});
}
export function unbindOwnerGroup(channel: OwnerChannelName, groupRef: string): Promise<unknown> {
  return _del(groupPath(channel, groupRef));
}
export function clearGroupRecords(channel: OwnerChannelName, groupRef: string): Promise<unknown> {
  return _post(groupPath(channel, groupRef) + '/clear');
}
export function nameGroupMember(channel: OwnerChannelName, groupRef: string, senderRef: string, label: string): Promise<unknown> {
  return _put(groupPath(channel, groupRef) + '/members/' + encodeURIComponent(senderRef), {label});
}
export function setFamilyNote(channel: OwnerChannelName, groupRef: string, text: string): Promise<unknown> {
  return _put(groupPath(channel, groupRef) + '/family-note', {text});
}
export function deleteFamilyMemory(channel: OwnerChannelName, groupRef: string, entryId?: string): Promise<unknown> {
  return _del(groupPath(channel, groupRef) + '/family-memory' + (entryId ? '?entry_id=' + encodeURIComponent(entryId) : ''));
}

// ── The family lists (step 25 C4, design 7.9.5). Owner session only. Step 23.1: each group has its
//    own two lists; everyone in that group sees everything on them; adding passes the group's exit
//    check (422 "withheld: <kind>"). A group is {channel, group_ref}; `unassigned` the items no group
//    owns (made before step 23.1). An unbound group's lists are shown and removed, never added to.
export type FamilyListName = 'shopping' | 'todo';
export interface FamilyListItem {
  item_id: string; list: FamilyListName; text: string; added_by: string | null; added_by_role: 'owner' | 'member' | 'steward';
  added_via: 'group' | 'owner_dm' | 'dashboard'; added_at: string; done: boolean; done_at: string | null; done_by: string | null;
}
export interface FamilyListGroup { channel: OwnerChannelName; group_ref: string; label: string | null; bound: boolean }
export type FamilyListScope = {channel: OwnerChannelName; group_ref: string} | 'unassigned';
const listQuery = (scope: FamilyListScope, first = '?') => scope === 'unassigned' ? first + 'unassigned=true'
  : first + 'channel=' + encodeURIComponent(scope.channel) + '&group_ref=' + encodeURIComponent(scope.group_ref);
/** The groups that have lists to show (bound ones, unbound ones that keep items) and the count of unassigned items. */
export function listFamilyListGroups(): Promise<{groups: FamilyListGroup[]; unassigned: number}> {
  return _get('/api/steward/lists/groups');
}
// At most 100 checked items of a list are shown, newest first; done_total counts a list's checked
// items and done_more how many of them are not shown (director 505).
export function listFamilyLists(scope: FamilyListScope): Promise<{group: FamilyListGroup | null; lists: Record<FamilyListName, FamilyListItem[]>;
  done_total?: Partial<Record<FamilyListName, number>>; done_more?: Partial<Record<FamilyListName, number>>}> {
  return _get('/api/steward/lists' + listQuery(scope));
}
export function addFamilyListItems(scope: FamilyListScope, list: FamilyListName, items: string[]): Promise<{items: FamilyListItem[]}> {
  return _post('/api/steward/lists/' + encodeURIComponent(list) + '/items' + listQuery(scope), {items});
}
export function checkFamilyListItems(scope: FamilyListScope, itemIds: string[], done: boolean): Promise<unknown> {
  return _post('/api/steward/lists/check' + listQuery(scope), {item_ids: itemIds, done});
}
export function removeFamilyListItem(scope: FamilyListScope, itemId: string): Promise<unknown> {
  return _del('/api/steward/lists/items/' + encodeURIComponent(itemId) + listQuery(scope));
}
/** Unassigned items join a bound group's lists (review F2); `unknown` names ids that were not unassigned. */
export function assignFamilyListItems(group: {channel: OwnerChannelName; group_ref: string}, itemIds: string[]): Promise<{assigned: string[]; unknown: string[]}> {
  return _post('/api/steward/lists/assign' + listQuery(group), {item_ids: itemIds});
}
export function clearDoneFamilyListItems(scope: FamilyListScope, list?: FamilyListName): Promise<{cleared: number}> {
  return _post('/api/steward/lists/clear-done' + listQuery(scope) + (list ? '&list_name=' + encodeURIComponent(list) : ''));
}

/** Where a steward-conversation message came from ('in') or was mirrored to ('out'). */
export interface MessageSource {
  direction: 'in' | 'out'; channel: OwnerChannelName; sender_ref: string | null;
  channel_message_ref: string | null; at: string; error: string | null;
  /** When the owner sent it from the chat app (the platform's time, UTC); inbound only. */
  channel_at?: string | null;
}
/** At most this many ids per request (the backend's bound). */
export const MESSAGE_SOURCES_BATCH = 200;
/** An id absent from the map came from (or stayed on) the dashboard. */
export function stewardMessageSources(ids: string[]): Promise<Record<string, MessageSource>> {
  return _get<{sources?: Record<string, MessageSource>}>(
    '/api/steward/message-sources?ids=' + ids.map(encodeURIComponent).join(','))
    .then(r => r.sources ?? {});
}

// ── The steward's own MCP connections (step 24.1a, design 7.8.10). Owner session
//    only; a header secret goes in and is never answered back (`secret` is set/unset).
export type StewardVisibility = 'private' | 'public';
export interface StewardConnection {
  conn_id: string; label: string; url: string; auth_kind: 'none' | 'header' | 'oauth';
  header_name: string | null; visibility: StewardVisibility; enabled: boolean;
  generation: number; config_version: number; secret: 'set' | 'unset' | 'oauth_needs_consent';
  auth_identity: string | null; identity_confirmed?: boolean; created_at: string; updated_at: string;
  last_probe_at: string | null; last_error: string | null; last_change: string; usable: boolean;
  tool_counts: {all: number; allowed: number; outward: number; changed: number; new: number};
}
export interface StewardRestartState {
  due: {driver_id: string; reason: string; due_at: string; created_at: string}[];
  done: {id: number; driver_id: string; reasons: {reason: string; effective_hash: string}[];
    restarted_at: string; error: string | null; noticed_at: string | null}[];
}
export interface StewardConnections {
  connections: StewardConnection[]; effective_tools: string[]; effective_hash: string; restart: StewardRestartState;
}
export interface StewardConnectionTool {
  raw_name: string; gw_name: string; name: string; description: string; input_schema: unknown;
  annotations: Record<string, unknown> | null; schema_sha256: string;
  allowed: boolean; outward: boolean; egress: boolean; blocked_args: string[]; daily_cap: number | null;
  state: 'new' | 'allowed' | 'off' | 'changed' | 'collision';
}
export interface StewardConnectionTools {
  conn_id: string; instructions: string | null; last_probe_at: string | null; last_error: string | null;
  tools: StewardConnectionTool[]; error?: string;
}
export interface StewardConnectionInput {
  conn_id: string; label: string; url: string; auth_kind: 'none' | 'header' | 'oauth';
  header_name?: string | null; visibility: StewardVisibility; enabled: boolean;
}
export type StewardToolChange = {raw_name: string} & Partial<Pick<StewardConnectionTool,
  'allowed' | 'outward' | 'egress' | 'blocked_args' | 'daily_cap' | 'schema_sha256'>>;
export function listStewardConnections(): Promise<StewardConnections> {
  return _get('/api/steward/connections');
}
export function saveStewardConnection(input: StewardConnectionInput): Promise<StewardConnection> {
  return _post('/api/steward/connections', input);
}
export function deleteStewardConnection(connId: string): Promise<unknown> {
  return _del('/api/steward/connections/' + encodeURIComponent(connId));
}
export function stewardConnectionTools(connId: string): Promise<StewardConnectionTools> {
  return _get('/api/steward/connections/' + encodeURIComponent(connId) + '/tools');
}
export function setStewardConnectionTools(connId: string, tools: StewardToolChange[]): Promise<StewardConnectionTools> {
  return _put('/api/steward/connections/' + encodeURIComponent(connId) + '/tools', {tools});
}
export function setStewardConnectionSecret(connId: string, value: string | null): Promise<{secret: 'set' | 'unset'}> {
  return _put('/api/steward/connections/' + encodeURIComponent(connId) + '/secret', {value});
}
export function probeStewardConnection(connId: string): Promise<StewardConnectionTools> {
  return _post('/api/steward/connections/' + encodeURIComponent(connId) + '/probe');
}

// ── OAuth for a connection and the confirmations of outward actions (step 24.1b,
//    design 7.8.6, 7.8.9). Owner session only; a client secret is never answered back.
export interface StewardConfirmation {
  code: string; conn_id: string; raw_name: string; gw_name: string; requester_kind: 'owner' | 'member';
  channel: string; status: string; created_at: string; confirmed_at: string | null; consumed_at: string | null;
  result: {error?: string} | null; message_text: string;
}
export function setStewardOAuthClient(connId: string, clientId: string | null, clientSecret: string | null): Promise<unknown> {
  return _put('/api/steward/connections/' + encodeURIComponent(connId) + '/oauth/client',
    {client_id: clientId, client_secret: clientSecret});
}
export function startStewardOAuth(connId: string): Promise<{conn_id: string; authorize_url: string}> {
  return _post('/api/steward/connections/' + encodeURIComponent(connId) + '/oauth/start');
}
export function confirmStewardIdentity(connId: string, identity: string): Promise<StewardConnection> {
  return _post('/api/steward/connections/' + encodeURIComponent(connId) + '/identity', {identity});
}
export function signOutStewardOAuth(connId: string): Promise<StewardConnection> {
  return _post('/api/steward/connections/' + encodeURIComponent(connId) + '/oauth/signout');
}
export function listStewardConfirmations(): Promise<{confirmations: StewardConfirmation[]}> {
  return _get('/api/steward/confirmations');
}
