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

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
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
    throw new ApiError(
      403,
      'Read-only access — sign in as an authorized user to make changes.',
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

    if (body !== undefined && body !== null && (method === 'POST' || method === 'PATCH')) {
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

  // Non-OK → parse error detail from body
  return response.json().then(
    (errData: Record<string, unknown>) => {
      const message =
        (errData?.detail as string) ||
        (errData?.message as string) ||
        response.statusText ||
        'Request failed';
      throw new ApiError(response.status, String(message));
    },
    () => {
      throw new ApiError(response.status, response.statusText || 'Request failed');
    },
  );
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

export function getRunDetail(runId: string): Promise<Record<string, unknown>> {
  return _get('/api/runs/' + encodeURIComponent(runId) + '/detail');
}

// ═════════════════════════════════════════════════════════════════════
//  Chat
// ═════════════════════════════════════════════════════════════════════

export function sendChatMessage(
  projectId: string,
  message: string,
): Promise<Record<string, unknown>> {
  return _post('/api/agent/chat/message', {
    session_id: '',
    project_id: projectId,
    role: 'user',
    content: message,
  });
}

/**
 * Create a new chat session and return its ID.
 * POST /api/agent/session/create
 */
export function createSession(): Promise<{ session_id: string }> {
  return _post('/api/agent/session/create');
}

/**
 * Get the full message history for a session (oldest-first).
 * GET /api/agent/chat/history?session_id=...
 */
export function getChatHistory(
  sessionId: string,
): Promise<{ session_id: string; messages: Record<string, unknown>[] }> {
  return _get(
    '/api/agent/chat/history?session_id=' + encodeURIComponent(sessionId),
  );
}

/**
 * List chat sessions with message count and preview.
 * GET /api/agent/sessions?limit=200&project_id=...
 *
 * @param projectId — optional project_id filter (pass null for all sessions)
 */
export function listSessions(
  projectId?: string | null,
): Promise<{ sessions: Record<string, unknown>[] }> {
  let path = '/api/agent/sessions?limit=200';
  if (projectId) {
    path += '&project_id=' + encodeURIComponent(projectId);
  }
  return _get(path);
}

// ═════════════════════════════════════════════════════════════════════
//  Trace
// ═════════════════════════════════════════════════════════════════════

export interface TraceQueryOptions {
  category?: string;
  afterSeq?: number;
  order?: 'asc' | 'desc';
  limit?: number;
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
  if (opts?.afterSeq != null) params.push('after_seq=' + opts.afterSeq);
  if (opts?.order) params.push('order=' + opts.order);
  if (opts?.limit != null) params.push('limit=' + opts.limit);
  if (params.length > 0) path += '?' + params.join('&');
  return _get(path);
}

// ═════════════════════════════════════════════════════════════════════
//  All Runs (cross-project)
// ═════════════════════════════════════════════════════════════════════

/** List all runs across all projects, optionally filtered by status (e.g. 'running'). */
export function listAllRuns(status?: string): Promise<{ runs: Record<string, unknown>[] }> {
  let path = '/api/runs';
  if (status) path += '?status=' + encodeURIComponent(status);
  return _get(path);
}

// ═════════════════════════════════════════════════════════════════════
//  Checkpoints
// ═════════════════════════════════════════════════════════════════════

export function approveCheckpoint(projectId: string, feedback?: string): Promise<void> {
  const body: Record<string, unknown> = { checkpoint: '', project_id: projectId };
  if (feedback) {
    body.feedback = feedback;
  }
  return _post(
    '/api/meta/' + encodeURIComponent(projectId) + '/checkpoint/approve',
    body,
  );
}

export function rejectCheckpoint(projectId: string, feedback: string): Promise<void> {
  return _post('/api/meta/' + encodeURIComponent(projectId) + '/checkpoint/reject', {
    checkpoint: '',
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
}> {
  return _get('/api/me');
}

/** Client-side write permission toggle (no network call). */
export function setCanWrite(value: boolean): void {
  authStore.set({
    canWrite: value,
    email: get(authStore).email,
    permissionResolved: true,
  });
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
 * Re-run Researcher + Architect planning steps.
 * POST /api/projects/{projectId}/refresh-planning
 */
export function refreshPlanning(projectId: string): Promise<Record<string, unknown>> {
  return _post('/api/projects/' + encodeURIComponent(projectId) + '/refresh-planning');
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
