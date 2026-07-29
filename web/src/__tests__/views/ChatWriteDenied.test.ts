/**
 * Chat.svelte must tell a read-only user WHY the send failed.
 *
 * The chat POST streams SSE, so it uses raw fetch instead of the api.ts
 * wrapper. It treated every non-OK response as a broken stream: a 403 was
 * retried like a flaky network and then reported as "Connection error: HTTP
 * 403". Now the response is classified through api.ts (errorFromResponse /
 * errorMessageKey) — a server refusal is not retried and is shown as
 * translated text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';
import { t } from '../../lib/i18n.svelte';

// ── Mock API module ─────────────────────────────────────────────────

const mockApi = vi.hoisted(() => ({
  createSession: vi.fn(),
  getChatHistory: vi.fn(),
  listSessions: vi.fn(),
}));

// Partial mock — the real ApiError/errorFromResponse/errorMessageKey are what
// this test is about.
vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  ...mockApi,
}));

vi.mock('svelte-spa-router', () => ({
  push: vi.fn(),
  default: vi.fn(),
}));

import Chat from '../../views/Chat.svelte';

function denialResponse(code: string, detail: string): Response {
  return new Response(JSON.stringify({ detail, code }), {
    status: 403,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchSpy: ReturnType<typeof vi.spyOn>;

// This jsdom build ships a partial localStorage (no .clear) — install a real
// Map-backed one so the component's session persistence works in tests.
function installLocalStorage() {
  const store = new Map<string, string>();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    },
  });
}

beforeEach(() => {
  installLocalStorage();
  // canWrite:true — the SERVER refuses; the client has no idea yet. This is the
  // real read-only case: /api/me said yes, or was never resolved.
  authStore.set({ email: 'u@x', canWrite: true, permissionResolved: true, gateEnabled: true });
  connectionStore.set({ connectionOk: true, lastEventAt: Date.now() });
  mockApi.createSession.mockResolvedValue({ session_id: 'sess-1' });
  mockApi.getChatHistory.mockResolvedValue({ session_id: 'sess-1', mode: 'butler', messages: [] });
  mockApi.listSessions.mockResolvedValue({ sessions: [] });
  fetchSpy = vi.spyOn(globalThis, 'fetch');
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function sendMessage(container: HTMLElement, text: string) {
  const input = container.querySelector('#chat-input-field') as HTMLTextAreaElement;
  await fireEvent.input(input, { target: { value: text } });
  const btn = container.querySelector('#chat-send-btn') as HTMLButtonElement;
  await fireEvent.click(btn);
}

async function renderChat() {
  const { container } = render(Chat, { props: { params: {} } });
  await waitFor(() => {
    expect(container.querySelector('#chat-input-field')).toBeTruthy();
  });
  return container;
}

describe('Chat write denial', () => {
  it('shows the denial reason instead of a connection error', async () => {
    fetchSpy.mockResolvedValue(
      denialResponse('write_denied_not_a_writer', 'Your account has no write permission.'));

    const container = await renderChat();
    await sendMessage(container, 'hello');

    await waitFor(() => {
      expect(container.textContent).toContain(t('error.notAWriter'));
    });
    expect(container.textContent).not.toContain('Connection error');
    expect(container.textContent).not.toContain('HTTP 403');
  });

  it('does not retry a refusal', async () => {
    fetchSpy.mockResolvedValue(
      denialResponse('write_denied_not_authenticated', 'Not signed in.'));

    const container = await renderChat();
    await sendMessage(container, 'hello');

    await waitFor(() => {
      expect(container.textContent).toContain(t('error.notAuthenticated'));
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(container.textContent).not.toContain('retrying');
  });

  it('still reports a genuine network failure as a connection error', async () => {
    fetchSpy.mockRejectedValue(new TypeError('Failed to fetch'));

    const container = await renderChat();
    await sendMessage(container, 'hello');

    await waitFor(() => {
      expect(container.textContent).toContain(t('error.network'));
    }, { timeout: 5000 });
    // network failures ARE retried once
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
