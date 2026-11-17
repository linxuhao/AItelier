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
/** Selecting a DAG is the list's job now: click its card. The pane header only
 *  reports which DAG that is, so a link into it is the observable result. */
async function selectDag(view: any, id: string) {
  const card = [...view.container.querySelectorAll('.project-card')]
    .find((c: any) => (c.textContent ?? '').includes(id)) as HTMLElement | undefined;
  if (!card) throw new Error('no project card for ' + id);
  await fireEvent.click(card);
  await waitFor(() => {
    const link = view.container.querySelector('aside.conversation-pane .pane-project') as HTMLAnchorElement;
    expect(link?.getAttribute('href') ?? '').toContain(id);
  });
}
const dagHref = (view: any) =>
  (view.container.querySelector('aside.conversation-pane .pane-project') as HTMLAnchorElement | null)
    ?.getAttribute('href') ?? '';

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
    expect(card('running-old').querySelector('.driver-state')?.textContent).toBe('Busy');
    expect(card('idle-recent').querySelector('.driver-state')?.textContent).toBe('Ready');
    expect(card('stopped-new').querySelector('.driver-state')?.textContent).toBe('Stopped');
    // Each state carries its animated mark beside the label (hidden from screen readers).
    expect(card('running-old').querySelector('.driver-state .driver-state-mark')?.getAttribute('aria-hidden')).toBe('true');
    expect(card('idle-recent').querySelector('.driver-state')?.getAttribute('data-phase')).toBe('ready');
    // The badge is read from the unread endpoint, not counted in the browser.
    expect(card('running-old').querySelector('.unread')?.textContent).toBe('2 unread');
    expect(card('idle-recent').querySelector('.unread')).toBeNull();
    // A DAG with no driver row at all still reads as stopped rather than blank.
    expect(card('running-old').querySelector('.driver-state')?.getAttribute('data-phase')).toBe('busy');
  });
});

describe('the split conversation pane', () => {
  const aside = (view: any) => view.container.querySelector('aside.conversation-pane') as HTMLElement;

  it('uses the available workspace instead of a persisted chat width', async () => {
    storage.set('aitelier_dag_pane_width', '280');
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    expect(aside(view).style.width).toBe('');
    expect(view.queryByRole('separator')).toBeNull();
    expect(view.container.querySelector('.dag-split.workbench')).not.toBeNull();
  });

  it('keeps the current conversation while searching the project list', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    await selectDag(view, 'running-old');
    await fireEvent.input(view.getByRole('searchbox'), {target: {value: 'idle'}});
    await waitFor(() => expect(cards(view.container)).toEqual(['idle-recent', 'idle-older']));
    expect(dagHref(view)).toContain('running-old');
    expect(view.getByRole('tab', {name: 'Conversation'})).toBeTruthy();
  });

  it('keeps the selected DAG across two mode switches and does not reload the thread', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'running-old'});
    // Every DAG in the list is a way to select it - four cards, and no picker
    // anywhere; the card for the selected DAG says so.
    await waitFor(() => expect(view.container.querySelectorAll('.project-card').length).toBe(4));
    expect(view.container.querySelector('aside.conversation-pane select')).toBeNull();
    await selectDag(view, 'idle-recent');
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('idle-recent'));
    const callsAfterPick = api.dshDriverConversation.mock.calls.length;
    const selectedCard = () => [...view.container.querySelectorAll('.project-card')]
      .find(c => (c.textContent ?? '').includes('idle-recent')) as HTMLElement;
    expect(selectedCard().getAttribute('aria-pressed')).toBe('true');

    await fireEvent.click(view.getByRole('tab', {name: 'Run history'}));
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalledWith('idle-recent', undefined));
    expect(dagHref(view)).toContain('idle-recent');
    expect(selectedCard().getAttribute('aria-pressed')).toBe('true');
    expect(view.container.querySelector('[data-mode-panel="conversation"]')?.hasAttribute('hidden')).toBe(true);
    expect(view.container.querySelector('[data-mode-panel="run"]')?.hasAttribute('hidden')).toBe(false);

    await fireEvent.click(view.getByRole('tab', {name: 'Conversation'}));
    await waitFor(() => expect(aside(view).querySelector('[data-mode-panel="conversation"]')?.hasAttribute('hidden')).toBe(false));
    expect(dagHref(view)).toContain('idle-recent');
    // Switching back did not re-issue the page request: the thread the user was
    // reading is the same one, same position, without asking to reload.
    expect(api.dshDriverConversation.mock.calls.length).toBe(callsAfterPick);
    expect(api.dshDriverHistory.mock.calls.length).toBe(1);

    await fireEvent.click(view.getByRole('tab', {name: 'Run history'}));
    expect(api.dshDriverHistory.mock.calls.length).toBe(1);
    expect(dagHref(view)).toContain('idle-recent');
    expect(selectedCard().getAttribute('aria-pressed')).toBe('true');
  });
});

describe('workbench project drafts', () => {
  it('retains separate drafts and exact question context while switching projects', async () => {
    const question = {message_id:'q-message',thread_id:'q-message',project_id:'running-old',
      sender_kind:'driver',sender_driver_id:'dsh-running',subject:'A long report title',body:'Report',refs:{},
      created_at:'2026-11-14T21:00:00Z',reply_to_message_id:null,
      segments:[{id:'q1',kind:'question',text:'Should we continue?'}]};
    api.dshDriverConversation.mockImplementation(async (projectId: string) => ({project_id:projectId,
      messages:projectId==='running-old'?[question]:[],has_more:false,next_cursor:null,end_of_history:true,
      unread_count:0,matched_total:1}));
    const view = render(StateProjects);
    await view.findByRole('heading', {name:'running-old'});
    await selectDag(view,'running-old');
    await fireEvent.click(await view.findByRole('button',{name:'Answer this question: Should we continue?'}));
    await fireEvent.input(view.getByRole('textbox'),{target:{value:'Project A answer'}});
    await selectDag(view,'idle-recent');
    await waitFor(()=>expect((view.getByRole('textbox') as HTMLTextAreaElement).value).toBe(''));
    await fireEvent.input(view.getByRole('textbox'),{target:{value:'Project B draft'}});
    await selectDag(view,'running-old');
    await waitFor(()=>expect((view.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Project A answer'));
    expect(view.container.querySelector('[data-answering]')?.getAttribute('data-answer-segment-id')).toBe('q1');
    await fireEvent.click(view.getByRole('button',{name:'Send'}));
    await waitFor(()=>expect(api.sendDriverConversationMessage).toHaveBeenCalledWith('running-old','Project A answer',{
      replyToMessageId:'q-message',targetDriverId:'dsh-running',answers:[{message_id:'q-message',segment_id:'q1'}],
    }));
    await selectDag(view,'idle-recent');
    await waitFor(()=>expect((view.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Project B draft'));
    expect(view.container.querySelector('[data-answering]')).toBeNull();
  });
});
