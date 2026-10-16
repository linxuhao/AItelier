/**
 * Coding Mode session view in Chat.svelte:
 * - the panel names the SESSION working tree (never relabelled Source) and
 *   links the Source view explicitly;
 * - an approval sends back exactly the plan token the view rendered;
 * - an explicit commit request rides in the chat body, never a model field;
 * - a coding_handoff event is surfaced with its status.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';

const mockApi = vi.hoisted(() => ({
  createSession: vi.fn(),
  getChatHistory: vi.fn(),
  listSessions: vi.fn(),
  codingView: vi.fn(),
  codingTree: vi.fn(),
  codingFile: vi.fn(),
  codingPlanDecision: vi.fn(),
}));

vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  ...mockApi,
}));

const mockPush = vi.fn();
vi.mock('svelte-spa-router', () => ({
  push: (...args: unknown[]) => mockPush(...args),
  default: vi.fn(),
}));

import Chat from '../../views/Chat.svelte';

function sseResponse(events: Record<string, unknown>[]): Response {
  const payload = events.map((e) => `data: ${JSON.stringify(e)}\n`).join('\n') + '\n';
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

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

const TOKEN = { step_instance_id: 7, version: 3, completion_seq: 2, outputs_sha256: 'a'.repeat(64) };
let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installLocalStorage();
  window.localStorage.setItem('aitelier.chat.sessionId', 'sess-w');
  authStore.set({ email: 'u@x', canWrite: true, permissionResolved: true, gateEnabled: false });
  connectionStore.set({ connectionOk: true, lastEventAt: Date.now() });
  mockApi.createSession.mockResolvedValue({ session_id: 'sess-w' });
  mockApi.getChatHistory.mockResolvedValue({ session_id: 'sess-w', mode: 'coding', messages: [] });
  mockApi.listSessions.mockResolvedValue({ sessions: [] });
  mockApi.codingView.mockResolvedValue({
    view: 'working_tree', label: 'Working tree of session sess-w (not Source)',
    session_id: 'sess-w', project_id: 'proj-1', run_id: 'run-w', state: 'ready',
    plan: { state: 'pending', run_id: 'plan-1',
            checkpoint: { expected_checkpoint: TOKEN, plan: 'PLAN TEXT V2', label: 'Plan Review' } },
    handoffs: [],
  });
  mockApi.codingTree.mockResolvedValue({ files: ['hello.py', 'lib/util.py'], run_id: 'run-w' });
  mockApi.codingFile.mockResolvedValue({ path: 'hello.py', content: "print('hello world')\n" });
  mockApi.codingPlanDecision.mockResolvedValue({ status: 'applied' });
  fetchSpy = vi.spyOn(globalThis, 'fetch');
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Chat coding session view', () => {
  it('shows the session working tree, not Source, and links Source explicitly', async () => {
    const { container } = render(Chat, { props: { params: {} } });
    const panel = await waitFor(() => {
      const el = container.querySelector('.coding-session-view') as HTMLElement;
      expect(el).toBeTruthy();
      return el;
    });
    expect(panel.textContent).toContain('Session working tree (not Source)');
    const link = panel.querySelector('a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('#/projects/proj-1');
    expect(mockApi.codingTree).toHaveBeenCalledWith('sess-w', 'run-w');
    const file = await waitFor(() => {
      const b = [...panel.querySelectorAll('.coding-files button')]
        .find((x) => x.textContent === 'hello.py') as HTMLButtonElement;
      expect(b).toBeTruthy();
      return b;
    });
    await fireEvent.click(file);
    await waitFor(() => expect(container.querySelector('.coding-preview')?.textContent)
      .toContain('hello world'));
    expect(mockApi.codingFile).toHaveBeenCalledWith('sess-w', 'hello.py', 'run-w');
  });

  it('approves exactly the rendered plan token', async () => {
    const { container } = render(Chat, { props: { params: {} } });
    const approve = await waitFor(() => {
      const plan = container.querySelector('.coding-plan') as HTMLElement;
      expect(plan?.textContent).toContain('PLAN TEXT V2');
      return plan.querySelector('button') as HTMLButtonElement;
    });
    await fireEvent.click(approve);
    await waitFor(() => expect(mockApi.codingPlanDecision).toHaveBeenCalled());
    const body = mockApi.codingPlanDecision.mock.calls[0][0];
    expect(body.decision).toBe('approve');
    expect(body.session_id).toBe('sess-w');
    expect(body.expected_checkpoint).toEqual(TOKEN);
    expect(body.decision_id).toMatch(/^[0-9a-f]{24}$/);
  });

  it('sends an explicit commit request and surfaces the handoff', async () => {
    fetchSpy.mockResolvedValue(sseResponse([
      { type: 'coding_handoff', status: 'applied', paths: ['hello.py'] },
      { type: 'done', message: { role: 'assistant', content: 'done' } },
    ]));
    const { container } = render(Chat, { props: { params: {} } });
    const commit = await waitFor(() => {
      const el = container.querySelector('.coding-commit') as HTMLInputElement;
      expect(el).toBeTruthy();
      return el;
    });
    await fireEvent.input(commit, { target: { value: 'Fix typo' } });
    const input = container.querySelector('#chat-input-field') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'fix the typo' } });
    await fireEvent.click(container.querySelector('#chat-send-btn') as HTMLButtonElement);
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const body = JSON.parse(fetchSpy.mock.calls[0][1]!.body as string);
    expect(body.mode).toBe('coding');
    expect(body.commit_message).toBe('Fix typo');
    await waitFor(() => expect(container.textContent).toContain('Source handoff: applied'));
  });
});
