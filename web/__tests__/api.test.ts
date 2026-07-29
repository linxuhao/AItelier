/**
 * Tests for web/src/lib/api.ts — Fetch wrapper and public API methods.
 *
 * Mocks the global fetch function and the authStore/connectionStore
 * to verify request/error handling and read-only gating.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { authStore, setAuth } from '../src/stores/auth';
import { connectionStore } from '../src/stores/connection';
import {
  listProjects,
  createProject,
  getProject,
  ApiError,
  errorFromResponse,
  errorMessageKey,
} from '../src/lib/api';

// ── Mock global fetch ───────────────────────────────────────────────

let mockFetch: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockFetch = vi.fn();
  vi.stubGlobal('fetch', mockFetch);

  // Reset auth state to write-enabled (default for tests)
  setAuth({ canWrite: true, email: 'test@example.com' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ── Helpers ─────────────────────────────────────────────────────────

function mockResponse(data: unknown, status = 200, statusText = 'OK') {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: () => Promise.resolve(data),
  } as Response);
}

function mockErrorResponse(status = 500, detail = 'Internal Server Error') {
  return Promise.resolve({
    ok: false,
    status,
    statusText: 'Error',
    json: () => Promise.resolve({ detail }),
  } as Response);
}

// ── listProjects ────────────────────────────────────────────────────

describe('listProjects', () => {
  it('calls GET /api/projects and returns parsed JSON', async () => {
    const projects = [{ project_id: 'p1', name: 'Project 1' }];
    mockFetch.mockResolvedValue(mockResponse(projects));

    const result = await listProjects();
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/projects',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(result).toEqual(projects);
  });

  it('retries once on network error for GET', async () => {
    // First call fails with network error, second succeeds
    const projects = [{ project_id: 'p2' }];
    mockFetch
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(mockResponse(projects));

    const result = await listProjects();
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(result).toEqual(projects);
  });

  it('throws ApiError on non-OK response', async () => {
    mockFetch.mockResolvedValue(mockErrorResponse(404, 'Not found'));

    await expect(listProjects()).rejects.toThrow(ApiError);
    await expect(listProjects()).rejects.toMatchObject({
      status: 404,
      message: 'Not found',
    });
  });
});

// ── createProject ───────────────────────────────────────────────────

describe('createProject', () => {
  it('calls POST /api/projects with JSON body', async () => {
    const body = { name: 'New Project', brief: 'desc' };
    const response = { project_id: 'new-id', ...body };
    mockFetch.mockResolvedValue(mockResponse(response));

    const result = await createProject(body);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/projects',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(body),
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
        }),
      }),
    );
    expect(result).toEqual(response);
  });

  it('throws ApiError(0) on network failure', async () => {
    mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(createProject({ name: 'x' })).rejects.toThrow(ApiError);
    await expect(createProject({ name: 'x' })).rejects.toMatchObject({
      status: 0,
    });
  });

  it('calls setDisconnected on network failure', async () => {
    mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(createProject({ name: 'x' })).rejects.toThrow();
    const conn = get(connectionStore);
    expect(conn.connectionOk).toBe(false);
  });
});

// ── Read-only gating ────────────────────────────────────────────────

describe('read-only gating', () => {
  it('throws ApiError(403) when canWrite is false for POST', async () => {
    setAuth({ canWrite: false });
    await expect(createProject({ name: 'x' })).rejects.toThrow(ApiError);
    await expect(createProject({ name: 'x' })).rejects.toMatchObject({
      status: 403,
    });
    // fetch should NOT be called
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('allows GET requests even when canWrite is false', async () => {
    setAuth({ canWrite: false });
    mockFetch.mockResolvedValue(mockResponse([]));

    const result = await listProjects();
    expect(result).toEqual([]);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

// ── getProject ──────────────────────────────────────────────────────

describe('getProject', () => {
  it('calls GET /api/projects/{id}', async () => {
    const project = { project_id: 'p1', name: 'Test' };
    mockFetch.mockResolvedValue(mockResponse(project));

    const result = await getProject('p1');
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/projects/p1',
      expect.any(Object),
    );
    expect(result).toEqual(project);
  });
});

// ── ApiError ────────────────────────────────────────────────────────

describe('ApiError', () => {
  it('captures status and message', () => {
    const err = new ApiError(404, 'Not found');
    expect(err.status).toBe(404);
    expect(err.message).toBe('Not found');
    expect(err.name).toBe('ApiError');
    expect(err.code).toBeNull();
  });
});

// ── 403 vs. connection failure ──────────────────────────────────────
//
// A refusal the server answered with and a request that never reached it are
// different failures. Conflating them is what made a read-only user's denied
// write read as "connection error".

function mockDenial(code: string, detail = 'denied') {
  return Promise.resolve({
    ok: false,
    status: 403,
    statusText: 'Forbidden',
    json: () => Promise.resolve({ detail, code }),
  } as Response);
}

describe('write-denial codes', () => {
  it('carries the server denial code onto the ApiError', async () => {
    mockFetch.mockResolvedValue(
      mockDenial('write_denied_not_authenticated', 'Not signed in.'),
    );

    await expect(createProject({ name: 'x' })).rejects.toMatchObject({
      status: 403,
      code: 'write_denied_not_authenticated',
      message: 'Not signed in.',
    });
  });

  it('does NOT mark the connection down on a 403', async () => {
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
    mockFetch.mockResolvedValue(mockDenial('write_denied_not_a_writer'));

    await expect(createProject({ name: 'x' })).rejects.toThrow(ApiError);
    expect(get(connectionStore).connectionOk).toBe(true);
  });

  it('tags the client-side read-only refusal with the same codes', async () => {
    setAuth({ canWrite: false, email: 'reader@example.com' });
    await expect(createProject({ name: 'x' })).rejects.toMatchObject({
      status: 403,
      code: 'write_denied_not_a_writer',
    });

    setAuth({ canWrite: false, email: '' });
    await expect(createProject({ name: 'x' })).rejects.toMatchObject({
      status: 403,
      code: 'write_denied_not_authenticated',
    });
  });
});

describe('errorFromResponse', () => {
  it('reads detail + code from the body', async () => {
    const err = await errorFromResponse(
      (await mockDenial('write_denied_bad_admin_token', 'Bad token.')) as Response,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(403);
    expect(err.code).toBe('write_denied_bad_admin_token');
    expect(err.message).toBe('Bad token.');
  });

  it('falls back to the status line on a non-JSON body', async () => {
    const err = await errorFromResponse({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: () => Promise.reject(new SyntaxError('not json')),
    } as unknown as Response);
    expect(err.status).toBe(502);
    expect(err.code).toBeNull();
    expect(err.message).toBe('Bad Gateway');
  });
});

describe('errorMessageKey', () => {
  it('maps each denial code to its own key', () => {
    expect(errorMessageKey(new ApiError(403, '', 'write_denied_not_authenticated')))
      .toBe('error.notAuthenticated');
    expect(errorMessageKey(new ApiError(403, '', 'write_denied_not_a_writer')))
      .toBe('error.notAWriter');
    expect(errorMessageKey(new ApiError(403, '', 'write_denied_bad_admin_token')))
      .toBe('error.badAdminToken');
  });

  it('keeps a code-less 403 a refusal, not a connection error', () => {
    expect(errorMessageKey(new ApiError(403, 'Forbidden'))).toBe('error.forbidden');
    expect(errorMessageKey(new ApiError(403, 'Forbidden', 'something_new')))
      .toBe('error.forbidden');
  });

  it('reports only unanswered requests as connection errors', () => {
    expect(errorMessageKey(new ApiError(0, 'Network error'))).toBe('error.network');
    expect(errorMessageKey(new TypeError('Failed to fetch'))).toBe('error.network');
    expect(errorMessageKey(new ApiError(500, 'boom'))).toBe('error.requestFailed');
  });
});
