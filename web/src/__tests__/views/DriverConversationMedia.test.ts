/** Step 24.2b (design-steward 7.8.12a "面板组件", "输入区", "对话里", "显示"): the shared
 *  conversation component takes files and shows them.
 *
 *  - the Attach button, a drop on the composer and a pasted image each cause exactly one
 *    upload per file, on its own request, as soon as the file is picked;
 *  - a refused upload (too_large, type, rate_limited, staging_full) keeps its card with the
 *    reason and never goes into the message; empty text with a staged file can be sent;
 *  - the files live in the pane's draft table: they survive switching projects and back,
 *    an upload that ends after the switch lands in its own project, and a send clears them
 *    with the text. When the answer comes back after the user switched away and wrote new
 *    text, the new text stays but the sent upload_id leaves the draft, so the next send
 *    does not carry it again (the backend would refuse the whole message);
 *  - in the thread an image is an <img> whose src is the same-origin media route, a voice
 *    card has an <audio>, a file card links to the bytes, a refused one is its refusal line,
 *    and the same holds for a director's message.
 *
 *  The API is mocked except for `mediaUrl`, and the last describe drives the real
 *  `uploadConversationFile` / `sendDriverConversationMessage` against a fake transport.
 */
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
import type { DriverConversationEntry } from '../../lib/api';
import DriverConversation from '../../views/DriverConversation.svelte';
import DriverConversationPane from '../../views/DriverConversationPane.svelte';

const U1 = 'u_0123456789abcdef', U2 = 'u_fedcba9876543210';
const entry = (id: string, kind: 'driver' | 'user', extra: Partial<DriverConversationEntry> = {}): DriverConversationEntry => ({
  message_id: id, thread_id: id, project_id: 'alpha', sender_kind: kind,
  sender_driver_id: kind === 'driver' ? 'dsh-alpha' : null, subject: 'subject ' + id, body: 'body ' + id,
  refs: {}, reply_to_message_id: null, created_at: '2026-11-23T10:00:00Z', ...extra,
});
const page = (messages: DriverConversationEntry[]) => ({project_id: 'alpha', messages, has_more: false,
  next_cursor: null, end_of_history: true, matched_total: messages.length, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null});
const file = (name: string, type: string, size = 8) => new File([new Uint8Array(size)], name, {type});
const staged = (uploadId: string, f: File, kind = 'image') =>
  ({upload_id: uploadId, kind, mime: f.type, size: f.size, name: f.name});
const deferred = <T>() => Promise.withResolvers<T>();
const textbox = (view: any) => view.getByRole('textbox', {name: 'Write a message'}) as HTMLTextAreaElement;
const sendButton = (view: any) => view.getByRole('button', {name: 'Send'}) as HTMLButtonElement;
const cards = (container: HTMLElement) => [...container.querySelectorAll('[data-composer-attachments] li')] as HTMLElement[];
const picker = (view: any) => view.getByLabelText('Choose files to attach') as HTMLInputElement;
async function pick(view: any, ...files: File[]) {
  await fireEvent.change(picker(view), {target: {files}});
}
const projects = [
  {project_id: 'alpha', title: 'Alpha'}, {project_id: 'beta', title: 'Beta'},
] as never;

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.dshDriverConversation.mockResolvedValue(page([]));
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.stewardMessageSources.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('the composer takes files', () => {
  it('the Attach button opens the picker, and each picked file is uploaded once', async () => {
    const a = file('a.png', 'image/png'), b = file('notes.pdf', 'application/pdf');
    api.uploadConversationFile.mockImplementation(async (_p: string, f: File) => staged(f === a ? U1 : U2, f, f === a ? 'image' : 'file'));
    const view = render(DriverConversation, {projectId: 'alpha'});
    const click = vi.spyOn(picker(view), 'click');
    await fireEvent.click(view.getByRole('button', {name: 'Attach'}));
    expect(click).toHaveBeenCalledTimes(1);
    await pick(view, a, b);
    await waitFor(() => expect(cards(view.container).map(c => c.dataset.uploadId)).toEqual([U1, U2]));
    expect(api.uploadConversationFile).toHaveBeenCalledTimes(2);
    expect(api.uploadConversationFile.mock.calls.map(([p, f, n]) => [p, f, n])).toEqual([['alpha', a, 'a.png'], ['alpha', b, 'notes.pdf']]);
    expect(cards(view.container).map(c => c.querySelector('.attachment-name')?.textContent)).toEqual(['a.png', 'notes.pdf']);
  });

  it('a drop on the composer uploads each dropped file once', async () => {
    const a = file('shot.png', 'image/png');
    api.uploadConversationFile.mockResolvedValue(staged(U1, a));
    const view = render(DriverConversation, {projectId: 'alpha'});
    const composer = view.container.querySelector('[data-conversation-composer]') as HTMLElement;
    await fireEvent.dragOver(composer, {dataTransfer: {types: ['Files'], files: []}});
    await fireEvent.drop(composer, {dataTransfer: {types: ['Files'], files: [a]}});
    await waitFor(() => expect(cards(view.container)).toHaveLength(1));
    expect(api.uploadConversationFile).toHaveBeenCalledTimes(1);
    expect(api.uploadConversationFile.mock.calls[0].slice(0, 3)).toEqual(['alpha', a, 'shot.png']);
  });

  it('a pasted image is uploaded once; text on the clipboard is not a file', async () => {
    const shot = file('image.png', 'image/png');
    api.uploadConversationFile.mockResolvedValue(staged(U1, shot));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await fireEvent.paste(textbox(view), {clipboardData: {items: [
      {kind: 'string', type: 'text/plain', getAsFile: () => null},
      {kind: 'file', type: 'image/png', getAsFile: () => shot},
    ]}});
    await waitFor(() => expect(cards(view.container)).toHaveLength(1));
    expect(api.uploadConversationFile).toHaveBeenCalledTimes(1);
    expect(api.uploadConversationFile.mock.calls[0].slice(0, 2)).toEqual(['alpha', shot]);
    // Text alone uploads nothing.
    await fireEvent.paste(textbox(view), {clipboardData: {items: [{kind: 'string', type: 'text/plain', getAsFile: () => null}]}});
    expect(api.uploadConversationFile).toHaveBeenCalledTimes(1);
  });

  it('a refused upload shows its line and never enters the message', async () => {
    const codes = ['too_large', 'type', 'rate_limited', 'staging_full'];
    const files = codes.map((code, i) => file(code + '.bin', i ? 'application/octet-stream' : 'image/png'));
    const ok = file('ok.png', 'image/png');
    api.uploadConversationFile.mockImplementation(async (_p: string, f: File) => {
      const index = files.indexOf(f);
      if (index >= 0) throw new Error(codes[index]);
      return staged(U1, f);
    });
    const view = render(DriverConversation, {projectId: 'alpha'});
    await pick(view, ...files, ok);
    await waitFor(() => expect(cards(view.container).filter(c => c.dataset.refused)).toHaveLength(4));
    await waitFor(() => expect(cards(view.container).find(c => c.dataset.uploadId)?.dataset.uploadId).toBe(U1));
    const lines = cards(view.container).map(c => c.querySelector('.attachment-refusal')?.textContent ?? null);
    expect(lines).toEqual(['too large, not received', 'type not accepted', 'too many uploads this hour - try again later',
      'upload space is full - try again later', null]);
    await fireEvent.input(textbox(view), {target: {value: 'see attached'}});
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(1));
    expect(api.sendDriverConversationMessage.mock.calls[0][2].attachments).toEqual([U1]);
    // Every card goes with the send: the refused ones were never part of the message.
    await waitFor(() => expect(cards(view.container)).toHaveLength(0));
  });

  it('empty text with a staged file can be sent; a file still uploading holds the send back', async () => {
    const a = file('a.png', 'image/png');
    const upload = deferred<unknown>();
    api.uploadConversationFile.mockReturnValue(upload.promise);
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('No messages yet. Say something to your driver.');
    expect(sendButton(view).disabled).toBe(true);
    await pick(view, a);
    await waitFor(() => expect(cards(view.container)).toHaveLength(1));
    expect(view.container.querySelector('[data-composer-attachments] progress')).not.toBeNull();
    expect(sendButton(view).disabled).toBe(true);
    upload.resolve(staged(U1, a));
    await waitFor(() => expect(sendButton(view).disabled).toBe(false));
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage)
      .toHaveBeenCalledWith('alpha', '', {replyToMessageId: undefined, targetDriverId: 'dsh-alpha', attachments: [U1]}));
    await waitFor(() => expect(cards(view.container)).toHaveLength(0));
  });

  it('a plain text send is the same request it always was', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await fireEvent.input(textbox(view), {target: {value: 'hello'}});
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(1));
    expect(Object.keys(api.sendDriverConversationMessage.mock.calls[0][2])).not.toContain('attachments');
  });
});

describe('the files live in the pane\'s draft table', () => {
  it('survive switching projects and back, an upload ending after the switch lands in its own project, and a send clears them with the text', async () => {
    const a = file('a.png', 'image/png'), b = file('b.png', 'image/png');
    const late = deferred<unknown>();
    api.uploadConversationFile.mockImplementation((_p: string, f: File) => f === a ? Promise.resolve(staged(U1, a)) : late.promise);
    const view = render(DriverConversationPane, {projects, selected: 'alpha'});
    await waitFor(() => expect(textbox(view)).toBeTruthy());
    await fireEvent.input(textbox(view), {target: {value: 'two pictures'}});
    await pick(view, a, b);
    await waitFor(() => expect(cards(view.container).map(c => c.dataset.uploadId)).toEqual([U1, '']));
    await view.rerender({selected: 'beta'});
    await waitFor(() => expect(textbox(view).value).toBe(''));
    expect(cards(view.container)).toHaveLength(0);
    // The second upload finishes while beta is on screen: it is alpha's.
    late.resolve(staged(U2, b));
    await new Promise(done => setTimeout(done, 0));
    expect(cards(view.container)).toHaveLength(0);
    await view.rerender({selected: 'alpha'});
    await waitFor(() => expect(textbox(view).value).toBe('two pictures'));
    await waitFor(() => expect(cards(view.container).map(c => c.dataset.uploadId)).toEqual([U1, U2]));
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(1));
    expect(api.sendDriverConversationMessage.mock.calls[0].slice(0, 2)).toEqual(['alpha', 'two pictures']);
    expect(api.sendDriverConversationMessage.mock.calls[0][2].attachments).toEqual([U1, U2]);
    await waitFor(() => expect(textbox(view).value).toBe(''));
    expect(cards(view.container)).toHaveLength(0);
    // And they stay cleared after another round trip.
    await view.rerender({selected: 'beta'});
    await view.rerender({selected: 'alpha'});
    await waitFor(() => expect(textbox(view).value).toBe(''));
    expect(cards(view.container)).toHaveLength(0);
  });

  it('an answer that comes back after a switch keeps the new text but drops the sent upload_id', async () => {
    const a = file('a.png', 'image/png');
    api.uploadConversationFile.mockResolvedValue(staged(U1, a));
    const answer = deferred<unknown>();
    api.sendDriverConversationMessage.mockReturnValueOnce(answer.promise).mockResolvedValue({});
    const view = render(DriverConversationPane, {projects, selected: 'alpha'});
    await waitFor(() => expect(textbox(view)).toBeTruthy());
    await fireEvent.input(textbox(view), {target: {value: 'first'}});
    await pick(view, a);
    await waitFor(() => expect(cards(view.container).map(c => c.dataset.uploadId)).toEqual([U1]));
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(1));
    // Away before the answer, back, and a new message is started.
    await view.rerender({selected: 'beta'});
    await view.rerender({selected: 'alpha'});
    await waitFor(() => expect(textbox(view).value).toBe('first'));
    await fireEvent.input(textbox(view), {target: {value: 'second'}});
    answer.resolve({});
    await waitFor(() => expect(cards(view.container)).toHaveLength(0));
    expect(textbox(view).value).toBe('second');
    await fireEvent.click(sendButton(view));
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledTimes(2));
    expect(api.sendDriverConversationMessage.mock.calls[1][1]).toBe('second');
    expect(api.sendDriverConversationMessage.mock.calls[1][2].attachments).toBeUndefined();
  });
});

describe('attachments in the thread', () => {
  const M_IMG = 'm_00000000000000a1', M_VOICE = 'm_00000000000000b2', M_FILE = 'm_00000000000000c3';
  const route = (id: string) => '/api/dsh-drivers/alpha/conversation/media/' + id;
  it('renders thumbnails, a voice card with <audio>, a file card and the refusal line, all from the same-origin route', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('m2', 'driver', {attachments: [
        {id: M_IMG, kind: 'image', mime: 'image/png', size: 2048, name: 'frame.png'},
        {id: M_FILE, kind: 'file', mime: 'application/pdf', size: 3 * 1024 * 1024, name: 'report.pdf'},
      ]}),
      entry('m1', 'user', {body: '', attachments: [
        {id: U1, kind: 'image', mime: 'image/jpeg', size: 1000, name: 'cat.jpg'},
        {id: M_VOICE, kind: 'voice', mime: 'audio/ogg', size: 4000, name: 'voice-1.ogg'},
        {kind: 'image', refused: 'too_large'},
      ]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelectorAll('.message-attachments img')).toHaveLength(2));
    const own = view.container.querySelector('[data-message-id="m1"]') as HTMLElement;
    const director = view.container.querySelector('[data-message-id="m2"]') as HTMLElement;
    for (const [article, id] of [[own, U1], [director, M_IMG]] as const) {
      const img = article.querySelector('.attachment-image img') as HTMLImageElement;
      expect(img.getAttribute('src')).toBe(route(id));
      expect(new URL(img.src).origin).toBe(window.location.origin);
      const link = img.closest('a') as HTMLAnchorElement;
      expect(link.getAttribute('href')).toBe(route(id));
      expect(link.target).toBe('_blank');
    }
    const audio = own.querySelector(`[data-attachment-voice="${M_VOICE}"] audio`) as HTMLAudioElement;
    expect(audio).not.toBeNull();
    expect(audio.getAttribute('src')).toBe(route(M_VOICE));
    expect(audio.controls).toBe(true);
    const pdf = director.querySelector(`[data-attachment-file="${M_FILE}"]`) as HTMLElement;
    expect(pdf.querySelector('a')?.getAttribute('href')).toBe(route(M_FILE));
    expect(pdf.querySelector('a')?.getAttribute('download')).toBe('report.pdf');
    expect(pdf.textContent).toContain('3 MB');
    expect(own.querySelector('[data-attachment-refused="too_large"]')?.textContent).toBe('[Image too large, not received]');
  });

  it('a file the library no longer has says it expired', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('m1', 'driver', {attachments: [{id: M_IMG, kind: 'image', mime: 'image/png', size: 10, name: 'old.png'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    const img = await waitFor(() => {
      const found = view.container.querySelector('.attachment-image img') as HTMLImageElement;
      expect(found).not.toBeNull();
      return found;
    });
    await fireEvent.error(img);
    await waitFor(() => expect(view.container.querySelector(`[data-attachment-expired="${M_IMG}"]`)?.textContent)
      .toBe('[Image old.png expired]'));
    expect(view.container.querySelector('.attachment-image img')).toBeNull();
  });
});

describe('the transport', () => {
  class FakeXhr {
    static last: FakeXhr;
    method = ''; url = ''; timeout = 0; headers: Record<string, string> = {}; body: unknown = null;
    status = 0; statusText = ''; responseText = '';
    upload: {onprogress: any} = {onprogress: null};
    onload: (() => void) | null = null; onerror: (() => void) | null = null; ontimeout: (() => void) | null = null;
    constructor() { FakeXhr.last = this; }
    open(method: string, url: string) { this.method = method; this.url = url; }
    setRequestHeader(name: string, value: string) { this.headers[name] = value; }
    send(body: unknown) { this.body = body; }
    answer(status: number, body: unknown) { this.status = status; this.responseText = JSON.stringify(body); this.onload?.(); }
  }
  let real: typeof import('../../lib/api');
  beforeEach(async () => {
    real = await vi.importActual<typeof import('../../lib/api')>('../../lib/api');
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
  });

  it('uploads the raw bytes with the upload header, the name in its own header (never the URL) and a 120 s timeout', async () => {
    const a = file('my shot.png', 'image/png', 100);
    const progress = vi.fn();
    const done = real.uploadConversationFile('alpha', a, 'my shot.png', progress);
    const xhr = FakeXhr.last;
    expect(xhr.method).toBe('POST');
    expect(xhr.url).toBe('/api/dsh-drivers/alpha/conversation/uploads');
    expect(xhr.headers['X-AItelier-Upload']).toBe('1');
    expect(xhr.headers['X-AItelier-Upload-Name']).toBe('my%20shot.png');
    expect(xhr.timeout).toBe(120000);
    expect(xhr.body).toBe(a);
    xhr.upload.onprogress?.({lengthComputable: true, loaded: 50, total: 100});
    expect(progress).toHaveBeenCalledWith(0.5);
    xhr.answer(200, staged(U1, a));
    await expect(done).resolves.toEqual(staged(U1, a));
  });

  it('a refusal rejects with the server\'s code as the message', async () => {
    const done = real.uploadConversationFile('alpha', file('big.png', 'image/png'), 'big.png');
    FakeXhr.last.answer(413, {detail: 'too_large'});
    await expect(done).rejects.toMatchObject({status: 413, message: 'too_large'});
  });

  it('a read-only session is refused before any request', async () => {
    authStore.set({canWrite: false, permissionResolved: true, email: ''} as never);
    const before = FakeXhr.last;
    await expect(real.uploadConversationFile('alpha', file('a.png', 'image/png'), 'a.png')).rejects.toMatchObject({status: 403});
    expect(FakeXhr.last).toBe(before);
  });

  it('the message names its upload_ids only when it has some', async () => {
    const fetcher = vi.fn(async () => new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
    vi.stubGlobal('fetch', fetcher);
    await real.sendDriverConversationMessage('alpha', '', {attachments: [U1]});
    await real.sendDriverConversationMessage('alpha', 'text', {attachments: []});
    const bodies = fetcher.mock.calls.map((call: unknown[]) => JSON.parse((call[1] as RequestInit).body as string));
    expect(bodies[0].attachments).toEqual([U1]);
    expect(bodies[0].body).toBe('');
    expect('attachments' in bodies[1]).toBe(false);
    expect(real.mediaUrl('a b', U1)).toBe('/api/dsh-drivers/a%20b/conversation/media/' + U1);
  });
});
