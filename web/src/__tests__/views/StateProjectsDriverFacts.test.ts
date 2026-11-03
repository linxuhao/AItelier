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

const row = (id: string) => ({
  project_id: id, title: id, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: '2026-01-01T00:00:00+00:00',
});
const managed = (phase: string, enabled = 1, updated = '2026-06-01T00:00:00+00:00') =>
  ({enabled, phase, driver_id: 'dsh-' + phase, last_error: '', updated_at: updated});
// Global totals are deliberately different from every per-project row: a card
// that renders a global number cannot accidentally match.
const table = () => ({total: 9, online: 5, projects: {
  hot: {total: 3, online: 2, running_subagents: 4, running_pipelines: 2,
    managed: managed('running')},
  cold: {total: 1, online: 0, running_subagents: 0, running_pipelines: 0,
    managed: managed('waiting', 0)},
}});
const clone = (value: unknown) => JSON.parse(JSON.stringify(value));

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot'), row('cold')], next_after: null});
  api.driverStatus.mockImplementation(async () => clone(table()));
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue({project_id: 'hot', messages: [], has_more: false,
    next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0,
    read_upto_message_id: null, read_upto_created_at: null});
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-running', session_id: 's',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const cards = (container: HTMLElement) => [...container.querySelectorAll('.project-card')] as HTMLElement[];
const card = (container: HTMLElement, id: string) =>
  cards(container).find(c => c.querySelector('h2')?.textContent === id)!;
const fact = (container: HTMLElement, id: string, name: string) =>
  card(container, id).querySelector(`[data-fact="${name}"]`)?.textContent?.trim();
// The pane picks its first project before the driver table lands, so a test
// that cares WHICH project the control acts on says so explicitly.
async function pick(view: any, id: string) {
  const picker = view.getByLabelText('State DAG') as HTMLSelectElement;
  await waitFor(() => expect(picker.options.length).toBe(2));
  await fireEvent.change(picker, {target: {value: id}});
  await waitFor(() => expect(picker.value).toBe(id));
}

describe('the project button carries its own driver facts', () => {
  it('shows phase, driver count, running subagents and running pipelines per project', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    await waitFor(() => expect(fact(view.container, 'hot', 'subagents')).toBe('4'));

    // Running project: 2 of 3 drivers online, 4 subagents, 2 pipelines.
    expect(fact(view.container, 'hot', 'drivers')).toBe('2/3');
    expect(fact(view.container, 'hot', 'subagents')).toBe('4');
    expect(fact(view.container, 'hot', 'pipelines')).toBe('2');
    expect(card(view.container, 'hot').querySelector('.driver-state')?.textContent).toBe('Running');

    // Idle project: one known driver, nothing running. The numbers follow the
    // project, so they are not the global 9/5 in the same payload.
    expect(fact(view.container, 'cold', 'drivers')).toBe('0/1');
    expect(fact(view.container, 'cold', 'subagents')).toBe('0');
    expect(fact(view.container, 'cold', 'pipelines')).toBe('0');
    expect(card(view.container, 'cold').querySelector('.driver-state')?.textContent).toBe('Idle');
    expect(view.container.textContent).not.toContain('9/5');

    // The labels name the numbers instead of leaving four bare digits.
    const facts = card(view.container, 'hot').querySelector('.driver-facts') as HTMLElement;
    expect(facts.getAttribute('aria-label')).toBe('Driver and workload');
    expect(facts.textContent).toContain('drivers');
    expect(facts.textContent).toContain('subagents');
    expect(facts.textContent).toContain('pipelines');
  });

  it('replaces the standalone driver-status section, not the start control', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    await waitFor(() => expect(api.driverStatus).toHaveBeenCalled());
    // The collapsible table that used to sit above the list is gone ...
    expect(view.container.querySelector('.driver-status')).toBeNull();
    expect(view.container.querySelector('details')).toBeNull();
    expect(view.container.querySelector('tbody')).toBeNull();
    // ... and start/stop did not vanish with it: exactly ONE control, the one in
    // the conversation pane for the project selected there (the standalone
    // section rendered one per row).
    await pick(view, 'hot');
    await waitFor(() => expect(view.getAllByRole('button', {name: 'Stop driver'})).toHaveLength(1));
    await pick(view, 'cold');
    await waitFor(() => expect(view.getAllByRole('button', {name: 'Start driver'})).toHaveLength(1));
    expect(view.queryAllByRole('button', {name: 'Stop driver'})).toHaveLength(0);
    // One control, never one per row: the number of cards does not change it.
    expect(cards(view.container)).toHaveLength(2);
    expect(view.getAllByRole('button', {name: /^(Start|Stop) driver$/})).toHaveLength(1);
  });
});

describe('start and stop live in the conversation pane and act on its selection', () => {
  it('starts the project picked in the pane and refreshes the facts afterwards', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    // 'hot' is already enabled: the pane offers Stop for it.
    await pick(view, 'hot');
    await waitFor(() => expect(view.getByRole('button', {name: 'Stop driver'})).toBeTruthy());
    expect((view.getByRole('button', {name: 'Stop driver'}) as HTMLButtonElement).disabled).toBe(false);

    await pick(view, 'cold');
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('cold'));

    // The backend's answer changes once the start lands: State now reports the
    // driver as enabled and one pipeline running, which is what the pane and the
    // card must both re-read.
    let started = false;
    api.startDshDriver.mockImplementation(async () => { started = true; });
    api.driverStatus.mockImplementation(async () => {
      const next = table();
      if (started) next.projects.cold = {total: 1, online: 1, running_subagents: 0,
        running_pipelines: 1, managed: managed('starting')};
      return clone(next);
    });

    const start = view.getByRole('button', {name: 'Start driver'});
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(start);
    await waitFor(() => expect(api.startDshDriver).toHaveBeenCalledWith('cold'));
    expect(api.stopDshDriver).not.toHaveBeenCalled();

    // The pane re-reads State after the toggle: the button flips, and the card's
    // per-project facts follow the same payload.
    await waitFor(() => expect(view.getByRole('button', {name: 'Stop driver'})).toBeTruthy());
    await waitFor(() => expect(fact(view.container, 'cold', 'drivers')).toBe('1/1'));
    expect(fact(view.container, 'cold', 'pipelines')).toBe('1');
  });

  it('stops the selected project and reports a failed toggle instead of pretending', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    await pick(view, 'hot');
    await waitFor(() => expect(view.getByRole('button', {name: 'Stop driver'})).toBeTruthy());
    await fireEvent.click(view.getByRole('button', {name: 'Stop driver'}));
    await waitFor(() => expect(api.stopDshDriver).toHaveBeenCalledWith('hot'));

    api.stopDshDriver.mockRejectedValue(new Error('driver runtime unavailable'));
    await fireEvent.click(view.getByRole('button', {name: 'Stop driver'}));
    await waitFor(() => expect(view.getByRole('alert').textContent).toBe('driver runtime unavailable'));
  });
});
