import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

const entry = (id: string, kind: 'driver' | 'user', subject: string, body: string,
               created: string, extra: any = {}) =>
  ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: kind,
    sender_driver_id: kind === 'driver' ? 'dsh-alpha' : null, subject, body, refs: {},
    reply_to_message_id: null, created_at: created, ...extra});
const page = (messages: any[], extra: any = {}) =>
  ({project_id: 'alpha', messages, has_more: false, next_cursor: null, end_of_history: true,
    matched_total: messages.length, unread_count: 0, read_upto_message_id: null,
    read_upto_created_at: null, ...extra});
const texts = (nodes: NodeListOf<Element>) => [...nodes].map(node => node.textContent);

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.dshDriverConversation.mockResolvedValue(page([
    entry('m2', 'driver', 'Second report', 'all nodes dispatched', '2026-11-03T10:00:00+00:00'),
    entry('m1', 'user', 'Message from the user', 'start with node a', '2026-11-03T09:00:00+00:00'),
  ]));
});
afterEach(() => cleanup());

describe('one conversation, both directions', () => {
  it('renders the merged thread oldest-first with both senders', async () => {
    const view = render(DriverConversation, {projectId: 'alpha', projectTitle: 'Alpha'});
    await view.findByText('Second report');
    // The API pages newest-first; the thread reads oldest-first.
    expect(texts(view.container.querySelectorAll('article.message .subject')))
      .toEqual(['Message from the user', 'Second report']);
    expect(texts(view.container.querySelectorAll('article.message .sender')))
      .toEqual(['You', 'dsh-alpha']);
    expect(view.getByText('start with node a')).toBeTruthy();
    expect(view.container.textContent).toContain('Beginning of the conversation');
    // A read thread marks nothing and asks for no badge refresh.
    expect(api.markDriverConversationRead).not.toHaveBeenCalled();
  });

  it('shows unread messages, advances the marker only to what it showed, and reports it', async () => {
    const onread = vi.fn();
    api.dshDriverConversation.mockResolvedValue(page([
      entry('m3', 'driver', 'Third report', 'newest', '2026-11-03T11:00:00+00:00'),
      entry('m2', 'driver', 'Second report', 'older', '2026-11-03T10:00:00+00:00'),
    ], {unread_count: 2, read_upto_message_id: null}));
    render(DriverConversation, {projectId: 'alpha', onread});
    await waitFor(() => expect(api.markDriverConversationRead).toHaveBeenCalledWith('alpha', 'm3'));
    await waitFor(() => expect(onread).toHaveBeenCalled());
  });

  it('replies to one driver message and sends the link with the new message', async () => {
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('Second report');
    // Every driver message carries a reply affordance; the user's own does not.
    expect(view.getAllByRole('button', {name: /^Reply: /})).toHaveLength(1);
    await fireEvent.click(view.getByRole('button', {name: 'Reply: Second report'}));
    expect(view.getByText(/Replying to · Second report/)).toBeTruthy();
    await fireEvent.input(view.getByRole('textbox', {name: 'Write a message'}), {target: {value: 'node a is done'}});
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await waitFor(() => expect(api.sendDriverConversationMessage)
      .toHaveBeenCalledWith('alpha', 'node a is done',
        {replyToMessageId: 'm2', targetDriverId: 'dsh-alpha'}));
    await view.findByText('Sent');
    expect((view.getByRole('textbox', {name: 'Write a message'}) as HTMLTextAreaElement).value).toBe('');
    // The reply affordance is gone again: the next message is a new one.
    expect(view.queryByText(/Replying to · Second report/)).toBeNull();
  });

  it('sends a new message with no reply link, and cancels a chosen reply', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    await fireEvent.click(view.getByRole('button', {name: 'Reply: Second report'}));
    await fireEvent.click(view.getByRole('button', {name: 'Cancel reply'}));
    await fireEvent.input(view.getByRole('textbox', {name: 'Write a message'}), {target: {value: 'hello'}});
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await waitFor(() => expect(api.sendDriverConversationMessage)
      .toHaveBeenCalledWith('alpha', 'hello', {replyToMessageId: undefined, targetDriverId: undefined}));
  });

  it('loads older pages with the cursor, prepending them and stating the end of history', async () => {
    api.dshDriverConversation
      .mockResolvedValueOnce(page([
        entry('m2', 'driver', 'Second report', 'newest', '2026-11-03T10:00:00+00:00'),
      ], {has_more: true, next_cursor: 'cursor-1', end_of_history: false}))
      .mockResolvedValueOnce(page([
        entry('m0', 'driver', 'First report', 'oldest', '2026-11-03T08:00:00+00:00'),
      ], {has_more: false, next_cursor: null, end_of_history: true}));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    expect(view.container.textContent).not.toContain('Beginning of the conversation');
    await fireEvent.click(view.getByRole('button', {name: 'Load earlier messages'}));
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('alpha', 'cursor-1'));
    await view.findByText('First report');
    expect(texts(view.container.querySelectorAll('article.message .subject')))
      .toEqual(['First report', 'Second report']);
    expect(view.queryByRole('button', {name: 'Load earlier messages'})).toBeNull();
    expect(view.container.textContent).toContain('Beginning of the conversation');
  });

  it('shows the reply link that was persisted on a message', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('m2', 'user', 'Message from the user', 'answering you', '2026-11-03T10:00:00+00:00',
        {reply_to_message_id: 'abcdef123456'}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('answering you');
    expect(view.getByText(/Replying to #abcdef12/)).toBeTruthy();
  });
});
