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
const entry = (n: number) => ({
  message_id: 'm' + n, thread_id: 'thread', project_id: 'hot',
  sender_kind: n % 2 ? 'user' : 'driver', sender_driver_id: n % 2 ? null : 'dsh-hot',
  subject: 'Subject ' + n, body: 'Body of message ' + n, refs: {},
  reply_to_message_id: null, created_at: new Date(Date.UTC(2026, 0, 1, 0, n)).toISOString(),
});
const page = (count: number) => ({
  project_id: 'hot',
  messages: Array.from({length: count}, (_, i) => entry(count - i)),
  has_more: false, next_cursor: null, end_of_history: true,
  matched_total: count, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null,
});

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot'), row('cold')], next_after: null});
  // `hot` has a managed driver, `cold` has none: the same select drives both the
  // conversation and the start/stop control, so one picker covers both states.
  api.driverStatus.mockResolvedValue({total: 1, online: 1, projects: {
    hot: {total: 1, online: 1, running_subagents: 1, running_pipelines: 0,
      managed: {enabled: 1, phase: 'running', driver_id: 'dsh-hot', last_error: '',
        updated_at: '2026-06-01T00:00:00+00:00'}},
  }});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue(page(2));
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-hot', session_id: 's',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Selecting a DAG is the list's job now: click its card. */
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

async function mountPane() {
  const view = render(StateProjects);
  await view.findByRole('heading', {name: 'hot'});
  const pane = view.container.querySelector('aside.conversation-pane') as HTMLElement;
  // Until driver status lands both rows are idle, so which one the pane picks
  // first is not fixed: select the DAG under test explicitly, on its card.
  await selectDag(view, 'hot');
  await waitFor(() => expect(view.container.querySelectorAll('article.message').length).toBe(2));
  // `hot` is managed: wait until the toggle knows it before asserting its name.
  await waitFor(() => expect(view.getByRole('button', {name: 'Stop driver'})).toBeTruthy());
  const header = pane.querySelector('.pane-header') as HTMLElement;
  // The header used to hold the picker; it now holds the link into the DAG the
  // list selected, which is what "which DAG is this pane about" reads as.
  const dagLink = pane.querySelector('.pane-project') as HTMLAnchorElement;
  const thread = pane.querySelector('[data-conversation-thread]') as HTMLElement;
  const composer = pane.querySelector('[data-conversation-composer]') as HTMLElement;
  return {view, pane, header, thread, composer, dagLink};
}

describe('conversation workbench layout', () => {
  it('keeps conversation, question sidebar and composer in separate grid areas', async () => {
    const {pane,thread,composer}=await mountPane();
    const conversation=thread.parentElement!;
    expect(getComputedStyle(conversation).display).toBe('grid');
    expect(getComputedStyle(thread).overflowY).toBe('auto');
    expect(getComputedStyle(thread).minHeight).toBe('0');
    expect(getComputedStyle(pane).height).toBe('100%');
    expect(getComputedStyle(pane).overflowY).toBe('hidden');
    expect(thread.contains(composer)).toBe(false);
    expect(conversation.querySelector('[data-questions-box]')?.parentElement).toBe(conversation);
  });

  it('labels the driver control and links to the selected DAG', async () => {
    const {view,dagLink}=await mountPane();
    const stop=view.getByRole('button',{name:'Stop driver'});
    expect(stop.textContent?.trim()).toBe('Stop driver');
    expect(dagLink.getAttribute('href')).toContain('hot');
    await fireEvent.click(stop);
    await waitFor(()=>expect(api.stopDshDriver).toHaveBeenCalledWith('hot'));
    await selectDag(view,'cold');
    await fireEvent.click(view.getByRole('button',{name:'Start driver'}));
    await waitFor(()=>expect(api.startDshDriver).toHaveBeenCalledWith('cold'));
  });

  it('offers a multiline composer with an explicit keyboard shortcut', async () => {
    const {view,composer}=await mountPane();
    const input=view.getByRole('textbox',{name:'Write a message'}) as HTMLTextAreaElement;
    expect(input.rows).toBe(2);
    expect(getComputedStyle(input).resize).toBe('vertical');
    expect(getComputedStyle(input).maxHeight).toBe('160px');
    expect(composer.textContent).toContain('Ctrl / ⌘ + Enter');
    expect(view.getByRole('button',{name:'Send'}).closest('[data-conversation-composer]')).toBe(composer);
  });

  it('connects the active mode to its panel and retains the mounted conversation', async () => {
    const {view,thread}=await mountPane();
    const conversation=view.getByRole('tab',{name:'Conversation'});
    const run=view.getByRole('tab',{name:'Run history'});
    expect(conversation.getAttribute('aria-selected')).toBe('true');
    expect(view.container.querySelector('#'+conversation.getAttribute('aria-controls'))?.contains(thread)).toBe(true);
    await fireEvent.click(run);
    expect(run.getAttribute('aria-selected')).toBe('true');
    expect(conversation.getAttribute('aria-selected')).toBe('false');
    await fireEvent.click(conversation);
    expect(thread.isConnected).toBe(true);
    expect(view.container.querySelector('[data-conversation-thread]')).toBe(thread);
  });
});

describe('full screen (owner 2026-11-18: on a phone the chat had no room)', () => {
  it('the whole pane - conversation, history and tabs - covers the page, and leaves it on the button or Escape', async () => {
    const view = render(StateProjects);
    const toggle = await waitFor(() => {
      const button = view.container.querySelector('.fullscreen-toggle') as HTMLButtonElement;
      expect(button).toBeTruthy();
      return button;
    });
    const pane = view.container.querySelector('.conversation-pane') as HTMLElement;
    expect(pane.classList.contains('fullscreen')).toBe(false);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toBe('Full screen');
    await fireEvent.click(toggle);
    expect(pane.classList.contains('fullscreen')).toBe(true);
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe('Exit full screen');
    expect(document.body.style.overflow).toBe('hidden');
    // The tabs ride along: the mode buttons are inside the full-screen pane.
    expect(pane.querySelector('.pane-modes')).toBeTruthy();
    await fireEvent.keyDown(window, {key: 'Escape'});
    expect(pane.classList.contains('fullscreen')).toBe(false);
    expect(document.body.style.overflow).toBe('');
    await fireEvent.click(toggle);
    await fireEvent.click(toggle);
    expect(pane.classList.contains('fullscreen')).toBe(false);
  });
});
