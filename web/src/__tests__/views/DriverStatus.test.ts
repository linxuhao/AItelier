import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ driverStatus: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(), dshDriverHistory: vi.fn(), dshDriverMessages: vi.fn(), messageDshDriver: vi.fn(),
  dshDriverConversation: vi.fn(), dshDriverConversationUnread: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverStatus from '../../views/DriverStatus.svelte';
const projects: any[] = [{ project_id: 'alpha', title: 'Alpha' }, { project_id: 'beta', title: 'Beta' }];
const status = () => ({total: 4, online: 2, projects: {
  alpha: {total: 2, online: 1, running_subagents: 3, running_pipelines: 2,
    managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-alpha', last_error: ''}},
  beta: {total: 1, online: 0, running_subagents: 0, running_pipelines: 0},
}});
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.driverStatus.mockResolvedValue(status());
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-alpha', session_id: 'root-alpha', entries: [{seq: 5, type: 'tool/result', text: 'saved result', timestamp: 1}], has_more: true, next_before_seq: 5});
  api.dshDriverMessages.mockResolvedValue({project_id: 'alpha', messages: [], matched_total: 0, truncated: false});
  api.messageDshDriver.mockResolvedValue({});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue({project_id: 'alpha', messages: [], has_more: false, next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0, read_upto_message_id: null, read_upto_created_at: null});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.markDriverConversationRead.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => {cleanup(); vi.useRealTimers();});
describe('dashboard driver status', () => {
  it('shows global counts, per DAG workers and direct start/stop without opening a DAG', async () => {
    const view = render(DriverStatus, {projects, projectId: 'alpha'});
    await view.findByText('Total: 4 · Online: 2');
    expect(view.getByText('alpha: 1/2 Online · 3 subagents · 2 pipelines')).toBeTruthy();
    await fireEvent.click(view.container.querySelector('summary')!);
    expect(view.getByText('Alpha')).toBeTruthy(); expect(view.getByText('Beta')).toBeTruthy();
    await fireEvent.click(view.getByRole('button', {name: 'Stop driver'}));
    await waitFor(() => expect(api.stopDshDriver).toHaveBeenCalledWith('alpha'));
    await waitFor(() => expect(view.getByRole('button', {name: 'Start driver'}).hasAttribute('disabled')).toBe(false));
    await fireEvent.click(view.getByRole('button', {name: 'Start driver'}));
    await waitFor(() => expect(api.startDshDriver).toHaveBeenCalledWith('beta'));
  });
  it('refreshes running worker counts on the dashboard', async () => {
    vi.useFakeTimers();
    const view = render(DriverStatus, {projects, projectId: 'alpha'});
    await vi.advanceTimersByTimeAsync(1);
    expect(view.container.textContent).toContain('3 subagents');
    const next = status(); next.projects.alpha.running_subagents = 0; next.projects.alpha.running_pipelines = 1;
    api.driverStatus.mockResolvedValue(next);
    await vi.advanceTimersByTimeAsync(10000);
    expect(view.container.textContent).toContain('0 subagents · 1 pipelines');
  });
  it('keeps runtime details private and exposes real failures', async () => {
    authStore.set({canWrite: false, permissionResolved: true, email: null});
    const privateView = render(DriverStatus, {projects});
    expect(privateView.container.textContent).toBe(''); expect(api.driverStatus).not.toHaveBeenCalled();
    cleanup(); authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
    api.driverStatus.mockRejectedValue(new Error('runtime unavailable'));
    const view = render(DriverStatus, {projects});
    await view.findByRole('alert');
    expect(view.getByRole('alert').textContent).toBe('runtime unavailable');
  });
});

describe('driver records and realtime conversation', () => {
  it('sorts running before waiting and reorders after status refresh without changing the input', async () => {
    vi.useFakeTimers();
    const value: any = status(); value.projects.beta.managed = {enabled: 1, phase: 'running', driver_id: 'dsh-beta'};
    api.driverStatus.mockResolvedValue(value);
    const view = render(DriverStatus, {projects});
    await vi.advanceTimersByTimeAsync(1);
    expect([...view.container.querySelectorAll('tbody tr')].map(r => r.firstChild?.textContent)).toEqual(['Beta', 'Alpha']);
    expect(projects.map(p => p.project_id)).toEqual(['alpha', 'beta']);
    const next: any = status(); next.projects.alpha.managed.phase = 'running'; next.projects.beta.managed = {enabled: 1, phase: 'waiting'};
    api.driverStatus.mockResolvedValue(next);
    await vi.advanceTimersByTimeAsync(10000);
    expect([...view.container.querySelectorAll('tbody tr')].map(r => r.firstChild?.textContent)).toEqual(['Alpha', 'Beta']);
  });
  it('reads older records, sends to the exact driver and only reports inbox delivery', async () => {
    const view = render(DriverStatus, {projects});
    await view.findByText('Total: 4 · Online: 2');
    await fireEvent.click(view.getByRole('button', {name: 'Run history'}));
    await view.findByText('dsh-alpha · root-alpha');
    expect(api.dshDriverHistory).toHaveBeenCalledWith('alpha', undefined);
    expect(view.getByText('saved result')).toBeTruthy();
    await fireEvent.click(view.getByRole('button', {name: 'Older records'}));
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalledWith('alpha', 5));
    await fireEvent.input(view.getByRole('textbox', {name: 'Message driver'}), {target: {value: 'Please summarize progress'}});
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await waitFor(() => expect(api.messageDshDriver).toHaveBeenCalledWith('alpha', 'dsh-alpha', 'Please summarize progress'));
    await view.findByText('Sent to driver inbox');
    expect((view.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
  });
  it('appends live records once, catches up after reconnect and closes the stream', async () => {
    const streams: any[] = [];
    class Source {
      onopen: any; onerror: any; onmessage: any; close = vi.fn();
      constructor(public url: string) { streams.push(this); }
    }
    vi.stubGlobal('EventSource', Source);
    try {
      const view = render(DriverStatus, {projects});
      await view.findByText('Total: 4 · Online: 2');
      await fireEvent.click(view.getByRole('button', {name: 'Run history'}));
      await view.findByText('dsh-alpha · root-alpha');
      expect(streams[0].url).toContain(encodeURIComponent('dsh:dsh-alpha'));
      streams[0].onopen();
      await view.findByText('Live');
      const event = {data: JSON.stringify({log: JSON.stringify({type: 'driver_activity', phase: 'running', subagents: ['worker'], entry: {seq: 6, type: 'assistant/message', text: 'realtime reply', timestamp: 2}})})};
      streams[0].onmessage(event); streams[0].onmessage(event);
      await view.findByText('realtime reply');
      expect(view.getAllByText('realtime reply')).toHaveLength(1);
      streams[0].onerror(); await view.findByText('Reconnecting…');
      api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-alpha', session_id: 'root-alpha', entries: [{seq: 7, type: 'tool/result', text: 'missed while disconnected'}], has_more: true, next_before_seq: 7});
      streams[0].onopen(); await view.findByText('missed while disconnected');
      await fireEvent.click(view.getByRole('button', {name: 'Close history'}));
      expect(streams[0].close).toHaveBeenCalled();
    } finally {vi.unstubAllGlobals();}
  });
});

describe('one conversation with the driver', () => {
  const entry = (id: string, kind: 'driver' | 'user', subject: string, body: string, created: string, extra: any = {}) =>
    ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: kind,
      sender_driver_id: kind === 'driver' ? 'dsh-alpha' : null, subject, body, refs: {},
      reply_to_message_id: null, created_at: created, ...extra});
  const page = (messages: any[], extra: any = {}) =>
    ({project_id: 'alpha', messages, has_more: false, next_cursor: null, end_of_history: true,
      matched_total: messages.length, unread_count: 0, read_upto_message_id: null,
      read_upto_created_at: null, ...extra});

  it('shows the persisted unread badge, then clears it by reading the thread', async () => {
    api.dshDriverConversationUnread
      .mockResolvedValueOnce({projects: {alpha: 2}, total: 2})
      .mockResolvedValue({projects: {alpha: 0, beta: 0}, total: 0});
    api.dshDriverConversation.mockResolvedValue(page([
      entry('m2', 'driver', 'Second report', 'all nodes dispatched', '2026-11-03T10:00:00+00:00'),
      entry('m1', 'user', 'Message from the user', 'start with node a', '2026-11-03T09:00:00+00:00'),
    ], {unread_count: 2}));
    const view = render(DriverStatus, {projects});
    await view.findByText('Total: 4 · Online: 2');
    // beta has no managed runtime at all; every project still exposes its thread.
    expect(view.getAllByRole('button', {name: 'Conversation'})).toHaveLength(2);
    await waitFor(() => expect(view.container.querySelectorAll('.unread')).toHaveLength(1));
    expect(view.container.querySelector('.unread')!.textContent).toBe('2');

    await fireEvent.click(view.getAllByRole('button', {name: 'Conversation'})[0]);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('alpha'));
    await view.findByText('Second report');
    // Opening the thread advanced the persisted marker to the newest message it
    // showed - not to "now" - and the badge went away.
    await waitFor(() => expect(api.markDriverConversationRead).toHaveBeenCalledWith('alpha', 'm2'));
    await waitFor(() => expect(view.container.querySelector('.unread')).toBeNull());
    // One chronological order with both senders visible.
    expect([...view.container.querySelectorAll('article.message .subject')].map(e => e.textContent))
      .toEqual(['Message from the user', 'Second report']);
    expect([...view.container.querySelectorAll('article.message .sender')].map(e => e.textContent))
      .toEqual(['You', 'dsh-alpha']);
    // Reading a conversation is not reading a run: the history/live stream stays shut.
    expect(api.dshDriverHistory).not.toHaveBeenCalled();
    await fireEvent.click(view.getByRole('button', {name: 'Close messages'}));
    expect(view.container.querySelector('article.message')).toBeNull();
  });

  it('says so when the conversation is empty, and shows no badge for a read thread', async () => {
    const view = render(DriverStatus, {projects});
    await view.findByText('Total: 4 · Online: 2');
    await waitFor(() => expect(api.dshDriverConversationUnread).toHaveBeenCalled());
    expect(view.container.querySelector('.unread')).toBeNull();
    await fireEvent.click(view.getAllByRole('button', {name: 'Conversation'})[0]);
    await view.findByText('No messages yet. Say something to your driver.');
    expect(api.markDriverConversationRead).not.toHaveBeenCalled();
  });
});
