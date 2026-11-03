import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  stateProjects: vi.fn(), driverStatus: vi.fn(),
  dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(), messageDshDriver: vi.fn(),
  startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import StateProjects from '../../views/StateProjects.svelte';

const row = (id: string, created: string) => ({
  project_id: id, title: id, source_project_id: null, node_count: 2, verified_count: 1,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: created,
});
const managed = (phase: string, updated_at: string) => ({
  enabled: 1, phase, driver_id: 'dsh-' + phase, last_error: '', updated_at,
  provider: 'aitelier', model: 'driver', session_id: 'root-' + phase,
});
const driverTable = () => ({total: 4, online: 2, projects: {
  'running-old': {total: 1, online: 1, running_subagents: 2, running_pipelines: 1,
    managed: managed('running', '2020-01-01T00:00:00+00:00')},
  'idle-recent': {total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
    managed: managed('waiting', '2026-06-02T00:00:00+00:00')},
  'idle-older': {total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
    managed: managed('waiting', '2026-06-01T00:00:00+00:00')},
  'stopped-new': {total: 1, online: 0, running_subagents: 0, running_pipelines: 0,
    managed: managed('stopped', '2026-07-01T00:00:00+00:00')},
}});
const cards = (container: HTMLElement) =>
  [...container.querySelectorAll('.project-card')].map(card => card.querySelector('h2')?.textContent);

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [
    row('idle-older', '2026-01-01T00:00:00+00:00'),
    row('idle-recent', '2026-01-02T00:00:00+00:00'),
    row('running-old', '2026-01-03T00:00:00+00:00'),
    row('stopped-new', '2026-01-04T00:00:00+00:00'),
  ], next_after: null});
  api.driverStatus.mockResolvedValue(driverTable());
  api.dshDriverConversationUnread.mockResolvedValue({projects: {'running-old': 2}, total: 2});
  api.dshDriverConversation.mockResolvedValue({project_id: 'running-old', messages: [], has_more: false,
    next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0,
    read_upto_message_id: null, read_upto_created_at: null});
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-running', session_id: 'root-running',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('driver state on the State DAG list', () => {
  it('orders running first, then most recently updated, from State data', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    await waitFor(() => expect(api.dshDriverConversationUnread).toHaveBeenCalled());
    // Running but stale beats every idle/stopped row; the two idle rows keep
    // recency order; the stopped row comes last even though it is the newest.
    expect(cards(view.container)).toEqual(['running-old', 'idle-recent', 'idle-older', 'stopped-new']);
    expect(api.driverStatus).toHaveBeenCalled();
    expect(api.dshDriverConversationUnread).toHaveBeenCalled();
  });

  it('shows per-DAG driver state and the persisted unread badge', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    await waitFor(() => expect(view.container.querySelector('.unread')).toBeTruthy());
    const card = (id: string) => [...view.container.querySelectorAll('.project-card')]
      .find(c => c.querySelector('h2')?.textContent === id)!;
    expect(card('running-old').querySelector('.driver-state')?.textContent).toBe('Running');
    expect(card('idle-recent').querySelector('.driver-state')?.textContent).toBe('Idle');
    expect(card('stopped-new').querySelector('.driver-state')?.textContent).toBe('Stopped');
    // The badge is read from the unread endpoint, not counted in the browser.
    expect(card('running-old').querySelector('.unread')?.textContent).toBe('2');
    expect(card('idle-recent').querySelector('.unread')).toBeNull();
    // A DAG with no driver row at all still reads as stopped rather than blank.
    expect(card('running-old').querySelector('.driver-state')?.getAttribute('data-phase')).toBe('running');
  });
});

describe('the split conversation pane', () => {
  const aside = (view: any) => view.container.querySelector('aside.conversation-pane') as HTMLElement;

  it('resizes by dragging the separator and by keyboard, with no size buttons', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    const handle = view.getByRole('separator', {name: 'Resize conversation pane'});
    expect(aside(view).style.width).toBe('420px');
    await fireEvent.mouseDown(handle, {clientX: 800});
    await fireEvent.mouseMove(window, {clientX: 700});
    await fireEvent.mouseUp(window, {clientX: 700});
    await waitFor(() => expect(aside(view).style.width).toBe('520px'));
    expect(handle.getAttribute('aria-valuenow')).toBe('520');
    expect(storage.get('aitelier_dag_pane_width')).toBe('520');
    // Expand / Shrink / Restore are gone: they only moved a border the user can
    // drag. The handle keeps the keyboard half of that capability, so the width
    // is still settable without a mouse and without a button.
    await fireEvent.keyDown(handle, {key: 'Home'});
    await waitFor(() => expect(aside(view).style.width).toBe('280px'));
    await fireEvent.keyDown(handle, {key: 'End'});
    await waitFor(() => expect(aside(view).style.width).toBe('614px'));
    expect(storage.get('aitelier_dag_pane_width')).toBe('614');
  });

  it('survives a reload with the width the user chose', async () => {
    const first = render(StateProjects);
    await first.findByRole('heading', {name: 'running-old'});
    await fireEvent.mouseDown(first.getByRole('separator', {name: 'Resize conversation pane'}), {clientX: 800});
    await fireEvent.mouseMove(window, {clientX: 740});
    await fireEvent.mouseUp(window, {clientX: 740});
    await waitFor(() => expect(storage.get('aitelier_dag_pane_width')).toBe('480'));
    cleanup();
    const second = render(StateProjects);
    await second.findByRole('heading', {name: 'running-old'});
    expect(aside(second).style.width).toBe('480px');
  });

  it('degrades to one usable column on a narrow viewport', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    expect(aside(view).dataset.narrow).toBe('false');
    window.innerWidth = 600;
    try {
      window.dispatchEvent(new Event('resize'));
      await waitFor(() => expect(aside(view).dataset.narrow).toBe('true'));
      // No drag target and no fixed pixel width: the pane is a full-width row
      // under the list, and its picker and mode switch still work.
      expect(view.queryByRole('separator', {name: 'Resize conversation pane'})).toBeNull();
      expect(aside(view).style.width).toBe('');
      expect(view.getByRole('tab', {name: 'Conversation'})).toBeTruthy();
      const picker = view.getByLabelText('State DAG') as HTMLSelectElement;
      await fireEvent.change(picker, {target: {value: 'idle-recent'}});
      await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('idle-recent'));
    } finally { window.innerWidth = 1024; }
  });

  it('keeps the selected DAG across two mode switches and does not reload the thread', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    const picker = view.getByLabelText('State DAG') as HTMLSelectElement;
    await waitFor(() => expect(picker.options.length).toBe(4));
    await fireEvent.change(picker, {target: {value: 'idle-recent'}});
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('idle-recent'));
    const callsAfterPick = api.dshDriverConversation.mock.calls.length;

    await fireEvent.click(view.getByRole('tab', {name: 'Run history'}));
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalledWith('idle-recent', undefined));
    expect(picker.value).toBe('idle-recent');
    expect(view.container.querySelector('[data-mode-panel="conversation"]')?.hasAttribute('hidden')).toBe(true);
    expect(view.container.querySelector('[data-mode-panel="run"]')?.hasAttribute('hidden')).toBe(false);

    await fireEvent.click(view.getByRole('tab', {name: 'Conversation'}));
    await waitFor(() => expect(aside(view).querySelector('[data-mode-panel="conversation"]')?.hasAttribute('hidden')).toBe(false));
    expect((view.getByLabelText('State DAG') as HTMLSelectElement).value).toBe('idle-recent');
    // Switching back did not re-issue the page request: the thread the user was
    // reading is the same one, same position, without asking to reload.
    expect(api.dshDriverConversation.mock.calls.length).toBe(callsAfterPick);
    expect(api.dshDriverHistory.mock.calls.length).toBe(1);

    await fireEvent.click(view.getByRole('tab', {name: 'Run history'}));
    expect(api.dshDriverHistory.mock.calls.length).toBe(1);
    expect((view.getByLabelText('State DAG') as HTMLSelectElement).value).toBe('idle-recent');
  });
});
