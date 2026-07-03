/**
 * Tests for Chat.svelte coding-mode additions:
 * - the coding-mode toggle renders and is user-toggleable
 * - the /api/agent/chat body carries mode (butler|coding)
 * - a budget_exhausted SSE event surfaces a Continue button
 * - session mode from GET /chat/history sets the toggle
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';

// ── Mock API module ─────────────────────────────────────────────────

const mockApi = vi.hoisted(() => ({
  createSession: vi.fn(),
  getChatHistory: vi.fn(),
  listSessions: vi.fn(),
}));

vi.mock('../../lib/api', () => mockApi);

const mockPush = vi.fn();
vi.mock('svelte-spa-router', () => ({
  push: (...args: unknown[]) => mockPush(...args),
  default: vi.fn(),
}));

import Chat from '../../views/Chat.svelte';

// ── SSE fetch stub ──────────────────────────────────────────────────

function sseResponse(events: Record<string, unknown>[]): Response {
  const payload = events.map((e) => `data: ${JSON.stringify(e)}\n`).join('\n') + '\n';
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
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
  authStore.set({ email: 'u@x', canWrite: true, permissionResolved: true, gateEnabled: false });
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

describe('Chat coding mode', () => {
  it('renders the coding-mode toggle unchecked by default', async () => {
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      const toggle = container.querySelector('.coding-mode-toggle input') as HTMLInputElement;
      expect(toggle).toBeTruthy();
      expect(toggle.checked).toBe(false);
    });
  });

  it('sends mode=butler by default and mode=coding when toggled', async () => {
    fetchSpy.mockResolvedValue(
      sseResponse([{ type: 'done', message: { role: 'assistant', content: 'ok' } }]));
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      expect(container.querySelector('#chat-input-field')).toBeTruthy();
    });

    await sendMessage(container, 'hello');
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    let body = JSON.parse(fetchSpy.mock.calls[0][1]!.body as string);
    expect(body.mode).toBe('butler');

    // toggle coding mode, send again
    const toggle = container.querySelector('.coding-mode-toggle input') as HTMLInputElement;
    await fireEvent.click(toggle);
    await sendMessage(container, 'fix the bug');
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
    body = JSON.parse(fetchSpy.mock.calls[1][1]!.body as string);
    expect(body.mode).toBe('coding');
  });

  it('budget_exhausted event shows a Continue button that resends', async () => {
    fetchSpy.mockResolvedValueOnce(
      sseResponse([{ type: 'budget_exhausted', tool_turns: 50,
                     message: "Tool-turn budget (50) reached. Reply 'continue' to keep going." }]));
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      expect(container.querySelector('#chat-input-field')).toBeTruthy();
    });

    await sendMessage(container, 'big task');
    const continueBtn = await waitFor(() => {
      const btn = container.querySelector('.btn-continue') as HTMLButtonElement;
      expect(btn).toBeTruthy();
      return btn;
    });

    fetchSpy.mockResolvedValueOnce(
      sseResponse([{ type: 'done', message: { role: 'assistant', content: 'finished' } }]));
    await fireEvent.click(continueBtn);
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchSpy.mock.calls[1][1]!.body as string);
    expect(body.message).toBe('continue');
    // pause banner is cleared once the follow-up is sent
    await waitFor(() => {
      expect(container.querySelector('.btn-continue')).toBeFalsy();
    });
  });

  it('adopts the session mode returned by chat history', async () => {
    window.localStorage.setItem('aitelier.chat.sessionId', 'sess-9');
    mockApi.getChatHistory.mockResolvedValue({
      session_id: 'sess-9', mode: 'coding',
      messages: [{ role: 'user', content: 'earlier' }],
    });
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      const toggle = container.querySelector('.coding-mode-toggle input') as HTMLInputElement;
      expect(toggle?.checked).toBe(true);
    });
  });
});

describe('Chat cache-usage stats', () => {
  it('token_usage event renders cache ratio and billed tokens in the bar', async () => {
    fetchSpy.mockResolvedValue(sseResponse([
      { type: 'token_usage', tokens: 500, total_tokens: 500, limit: 1000,
        mode: 'coding', hit_ratio: 0.7, billed_tokens: 141000 },
      { type: 'done', message: { role: 'assistant', content: 'ok' } },
    ]));
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      expect(container.querySelector('#chat-input-field')).toBeTruthy();
    });

    await sendMessage(container, 'hello');
    await waitFor(() => {
      const label = container.querySelector('.token-bar-label');
      expect(label?.textContent).toContain('cache 70%');
      expect(label?.textContent).toContain('billed 141.0k');
    });
  });

  it('restores cache stats from chat history even with zero token counts', async () => {
    window.localStorage.setItem('aitelier.chat.sessionId', 'sess-9');
    mockApi.getChatHistory.mockResolvedValue({
      session_id: 'sess-9', mode: 'butler',
      messages: [{ role: 'user', content: 'earlier' }],
      token_count: 0, token_limit: 200000, total_tokens: 0,
      hit_ratio: 0.8, billed_tokens: 33000,
    });
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      const label = container.querySelector('.token-bar-label');
      expect(label?.textContent).toContain('cache 80%');
      expect(label?.textContent).toContain('billed 33.0k');
    });
  });

  it('bar omits cache stats when no usage recorded', async () => {
    fetchSpy.mockResolvedValue(sseResponse([
      { type: 'token_usage', tokens: 500, total_tokens: 500, limit: 1000,
        mode: 'coding' },
      { type: 'done', message: { role: 'assistant', content: 'ok' } },
    ]));
    const { container } = render(Chat, { props: { params: {} } });
    await waitFor(() => {
      expect(container.querySelector('#chat-input-field')).toBeTruthy();
    });

    await sendMessage(container, 'hello');
    await waitFor(() => {
      const label = container.querySelector('.token-bar-label');
      expect(label?.textContent).toContain('window');
      expect(label?.textContent).not.toContain('cache');
    });
  });
});
