/** Step 24.2b (design 7.8.12a "面板组件"): the steward's pane mounts the shared
 *  conversation component, so its conversation takes files and shows them - tested once
 *  here; the director's pane (StateProjects) is tested once in StateProjectsMedia. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn(),
  stewardMessageSources: vi.fn(), uploadConversationFile: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  dshDriverHistory: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import StewardPane from '../../views/StewardPane.svelte';

const HOME = 'aitelier-home', REF = 'm_0123456789abcdef', UPLOAD = 'u_0123456789abcdef';
const status = {total: 1, online: 1, projects: {[HOME]: {total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
  managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-home', last_error: '', model: '', role: 'steward'}}}} as never;

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.dshDriverConversation.mockResolvedValue({project_id: HOME, messages: [{
    message_id: 'm1', thread_id: 'm1', project_id: HOME, sender_kind: 'driver', sender_driver_id: 'dsh-home',
    subject: 'Image', body: 'here it is', refs: {}, reply_to_message_id: null, created_at: '2026-11-23T10:00:00Z',
    attachments: [{id: REF, kind: 'image', mime: 'image/png', size: 2048, name: 'image-01234567.png'}],
  }], has_more: false, next_cursor: null, end_of_history: true, matched_total: 1, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.stewardMessageSources.mockResolvedValue({});
});
afterEach(() => cleanup());

describe('the steward pane\'s conversation', () => {
  it('shows the steward\'s image from the media route and sends an uploaded file', async () => {
    const view = render(StewardPane, {projectId: HOME, status, onanswer: vi.fn()});
    const img = await waitFor(() => {
      const found = view.container.querySelector('.message-attachments img') as HTMLImageElement;
      expect(found).not.toBeNull();
      return found;
    });
    expect(img.getAttribute('src')).toBe(`/api/dsh-drivers/${HOME}/conversation/media/${REF}`);
    const photo = new File([new Uint8Array(4)], 'photo.jpg', {type: 'image/jpeg'});
    api.uploadConversationFile.mockResolvedValue({upload_id: UPLOAD, kind: 'image', mime: 'image/jpeg', size: 4, name: 'photo.jpg'});
    await fireEvent.change(view.getByLabelText('Choose files to attach'), {target: {files: [photo]}});
    await waitFor(() => expect(view.container.querySelector(`[data-upload-id="${UPLOAD}"]`)).not.toBeNull());
    expect(api.uploadConversationFile.mock.calls[0].slice(0, 2)).toEqual([HOME, photo]);
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(1));
    expect(api.sendDriverConversationMessage.mock.calls[0][0]).toBe(HOME);
    expect(api.sendDriverConversationMessage.mock.calls[0][2].attachments).toEqual([UPLOAD]);
  });
});
