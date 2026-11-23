/** Step 24.2b (design 7.8.12a "面板组件", "导演给业主发媒体" item 4): the director's pane on the
 *  State projects page mounts the same conversation component as the steward's
 *  (StewardPaneMedia), so a director's message with attachments renders the same and the
 *  owner's files go to that project's director - tested once here. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  stateProjects: vi.fn(), driverStatus: vi.fn(),
  dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(), messageDshDriver: vi.fn(),
  startDshDriver: vi.fn(), stopDshDriver: vi.fn(), uploadConversationFile: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import StateProjects from '../../views/StateProjects.svelte';

const PROJECT = 'game', REF = 'm_00000000000000aa', UPLOAD = 'u_00000000000000bb';
const row = {project_id: PROJECT, title: PROJECT, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''}, created_at: '2026-01-01T00:00:00Z'};

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.stateProjects.mockResolvedValue({projects: [row], next_after: null});
  api.driverStatus.mockResolvedValue({total: 1, online: 1, projects: {[PROJECT]: {total: 1, online: 1,
    running_subagents: 0, running_pipelines: 0, managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-game',
      last_error: '', updated_at: '2026-11-23T00:00:00Z'}}}});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue({project_id: PROJECT, messages: [{
    message_id: 'd1', thread_id: 'd1', project_id: PROJECT, sender_kind: 'driver', sender_driver_id: 'dsh-game',
    subject: 'Render', body: 'frame 12', refs: {}, reply_to_message_id: null, created_at: '2026-11-23T10:00:00Z',
    attachments: [{id: REF, kind: 'image', mime: 'image/png', size: 9000, name: 'frame-12.png'}],
  }], has_more: false, next_cursor: null, end_of_history: true, matched_total: 1, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('the director pane\'s conversation', () => {
  it('renders the director\'s image from the same-origin media route and sends an uploaded file to the director', async () => {
    const view = render(StateProjects);
    const img = await waitFor(() => {
      const found = view.container.querySelector('aside.conversation-pane .message-attachments img') as HTMLImageElement;
      expect(found).not.toBeNull();
      return found;
    });
    expect(img.getAttribute('src')).toBe(`/api/dsh-drivers/${PROJECT}/conversation/media/${REF}`);
    expect(new URL(img.src).origin).toBe(window.location.origin);
    const pdf = new File([new Uint8Array(4)], 'brief.pdf', {type: 'application/pdf'});
    api.uploadConversationFile.mockResolvedValue({upload_id: UPLOAD, kind: 'file', mime: 'application/pdf', size: 4, name: 'brief.pdf'});
    await fireEvent.change(view.getByLabelText('Choose files to attach'), {target: {files: [pdf]}});
    await waitFor(() => expect(view.container.querySelector(`[data-upload-id="${UPLOAD}"]`)).not.toBeNull());
    expect(api.uploadConversationFile.mock.calls[0].slice(0, 2)).toEqual([PROJECT, pdf]);
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledWith(PROJECT, '',
      {replyToMessageId: undefined, targetDriverId: 'dsh-game', attachments: [UPLOAD]}));
  });
});
