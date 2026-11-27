/** The owner's chat channels on the dashboard (plugin step 19, design 7.2).
 *
 *  - the steward pane has a Channels tab: each channel shows its bound account or
 *    "not bound", and the host's status when it reported one (with its time); a WeChat
 *    login QR is an <img> of a data URL, never markup, with the scan line;
 *  - while a channel is not connected and the tab is visible, the list is re-read
 *    every 15 s, so a login QR that appears later shows without a reload;
 *  - Bind asks the backend for a one-time code and shows it with a countdown and
 *    the instruction; the code goes away once the list shows a new binding;
 *  - Unbind asks first; the default-channel selector PUTs the choice (or null);
 *  - "New QR code" (WeChat, while not connected) asks the backend for a new login QR; the
 *    newest request's state is one line on the card, and the button waits while it is
 *    pending or while the bridge said to wait;
 *  - message source badges: only the steward's conversation asks for sources, once
 *    per new message id, batched; a director's pane asks for nothing.
 *
 *  Only `setInterval`/`clearInterval`/`setTimeout`/`Date` are faked where a test
 *  needs the clock; the API is mocked throughout.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn(),
  dshDriverHistory: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  listStewardProposals: vi.fn(), stewardProposalImpact: vi.fn(), listStewardSchedules: vi.fn(),
  listOwnerChannels: vi.fn(), createOwnerChannelCode: vi.fn(), unbindOwnerChannel: vi.fn(),
  setDefaultOwnerChannel: vi.fn(), stewardMessageSources: vi.fn(), requestOwnerChannelQr: vi.fn(),
  listOwnerGroups: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { DriverConversationEntry, OwnerChannelLoginRequest, OwnerChannels } from '../../lib/api';
import StewardChannels from '../../views/StewardChannels.svelte';
import StewardPane from '../../views/StewardPane.svelte';
import DriverConversationPane from '../../views/DriverConversationPane.svelte';

const channels = (over: Partial<OwnerChannels> = {}): OwnerChannels => ({
  channels: [
    {channel: 'slack', bound: true, sender_ref: 'U0OWNER', bound_at: '2026-11-18T10:00:00Z', revoked_at: null, is_default: 1,
      status: {connected: true}},
    {channel: 'wechat', bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: 0,
      status: {connected: false, detail: 'scan needed'}},
  ],
  default_channel: 'slack', ...over,
});
const card = (container: HTMLElement, channel: string) => container.querySelector(`[data-channel="${channel}"]`) as HTMLElement;
const frame = () => new Promise(done => requestAnimationFrame(() => done(null)));

const entry = (id: string, kind: 'driver' | 'user', created = '2026-11-18T10:00:00Z'): DriverConversationEntry => ({
  message_id: id, thread_id: 't', project_id: 'aitelier-home', sender_kind: kind,
  sender_driver_id: kind === 'driver' ? 'dsh-home' : null, subject: 's ' + id, body: 'body ' + id,
  refs: {}, reply_to_message_id: null, created_at: created,
});
const page = (messages: DriverConversationEntry[]) => ({project_id: 'aitelier-home', messages, has_more: false,
  next_cursor: null, end_of_history: true, matched_total: messages.length, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null});
const status = (role: 'steward' | 'director', id: string) => ({total: 1, online: 1, projects: {[id]: {total: 1, online: 1,
  running_subagents: 0, running_pipelines: 0, managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-' + id,
    last_error: '', model: '', role}}}}) as never;
const projectRow = (id: string) => ({project_id: id, title: id, source_project_id: null, node_count: 0, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''}, created_at: '2026-01-01T00:00:00Z'}) as never;
const sourceCalls = () => api.stewardMessageSources.mock.calls.map(([ids]) => ids as string[]);

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.listOwnerChannels.mockResolvedValue(channels());
  api.markDriverConversationRead.mockResolvedValue({});
  api.listStewardProposals.mockResolvedValue([]);
  api.listStewardSchedules.mockResolvedValue([]);
  api.stewardMessageSources.mockResolvedValue({});
  // The groups under each channel must read through this mock too: spreading the real api
  // module leaves every unlisted name real, and StewardGroups.onMount(load) would then issue
  // real GET /api/steward/groups whose deferred flush lands in a later test's fetch recorder.
  api.listOwnerGroups.mockResolvedValue({groups: [], groups_per_channel: 3});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const qrRequest = (over: Partial<OwnerChannelLoginRequest> = {}): OwnerChannelLoginRequest => ({
  request_id: 'qrreq-1', channel: 'wechat', status: 'pending', requested_at: '2026-11-18T12:00:00Z', completed_at: null,
  result: null, ...over,
});
const withQrRequest = (request: OwnerChannelLoginRequest | null, wechat: object = {}) => {
  const listed = channels();
  listed.channels[1] = {...listed.channels[1], login_request: request, ...wechat};
  return listed;
};
const feedback = (container: HTMLElement) => card(container, 'wechat').querySelector('[data-channel-qr-feedback]')?.textContent?.trim();
const newQr = (container: HTMLElement) => card(container, 'wechat').querySelector('[data-channel-new-qr]') as HTMLButtonElement | null;

describe('a new WeChat login QR', () => {
  it('shows "New QR code" on the WeChat card only, and only while it is not connected', async () => {
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    expect(newQr(view.container)?.textContent?.trim()).toBe('New QR code');
    expect(card(view.container, 'slack').querySelector('[data-channel-new-qr]')).toBeNull();
    cleanup();
    const connected = channels();
    connected.channels[1] = {...connected.channels[1], status: {connected: true}};
    api.listOwnerChannels.mockResolvedValue(connected);
    const again = render(StewardChannels);
    await waitFor(() => expect(card(again.container, 'wechat').querySelector('[data-channel-status]')?.getAttribute('data-channel-status'))
      .toBe('connected'));
    expect(newQr(again.container)).toBeNull();
  });

  it('pressing it asks the backend once, says it was requested, and waits while the request is pending; the list is re-read quickly', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    api.requestOwnerChannelQr.mockResolvedValue(qrRequest());
    const view = render(StewardChannels);
    await waitFor(() => expect(newQr(view.container)).not.toBeNull());
    await fireEvent.click(newQr(view.container)!);
    expect(api.requestOwnerChannelQr).toHaveBeenCalledTimes(1);
    expect(api.requestOwnerChannelQr).toHaveBeenCalledWith('wechat');
    await waitFor(() => expect(feedback(view.container)).toBe('Requested - waiting for the steward\'s host…'));
    expect(newQr(view.container)!.disabled).toBe(true);
    // Pending: the list is re-read on the short cadence; the host ran it, a QR is coming.
    api.listOwnerChannels.mockResolvedValue(withQrRequest(qrRequest({status: 'done', completed_at: '2026-11-18T12:00:03Z',
      result: {requested: true}}), {status: {connected: false, detail: 'QR requested'}}));
    await vi.advanceTimersByTimeAsync(6000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(feedback(view.container)).toBe('The QR is on its way: it shows up here in a few seconds.'));
    expect(newQr(view.container)!.disabled).toBe(false);
    // Still re-read quickly until the QR shows; then the line goes, the QR is the news.
    api.listOwnerChannels.mockResolvedValue(withQrRequest(qrRequest({status: 'done', completed_at: '2026-11-18T12:00:03Z',
      result: {requested: true}}), {qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>'}));
    await vi.advanceTimersByTimeAsync(6000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(3);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-qr] img')).not.toBeNull());
    expect(feedback(view.container)).toBeUndefined();
  });

  it('a request no host ran in time says so; each refusal is one plain line', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:03:00Z'));
    const cases: Array<[OwnerChannelLoginRequest, string]> = [
      [qrRequest({status: 'expired'}), 'The steward\'s host did not respond, so no QR was requested.'],
      [qrRequest({status: 'done', result: {refused: 'qr_waiting'}}), 'A QR is already waiting here: scan that one.'],
      [qrRequest({status: 'done', result: {refused: 'already_logged_in'}}), 'WeChat is already connected.'],
      [qrRequest({status: 'done', result: {refused: 'login_starting'}}), 'A QR is already on its way.'],
      [qrRequest({status: 'done', result: {refused: 'bridge_unreachable'}}), 'The WeChat bridge is not running.'],
      [qrRequest({status: 'done', result: {refused: 'not_supported'}}), 'The WeChat bridge is too old for this; rebuild it.'],
      [qrRequest({status: 'done', result: {refused: 'not_registered'}}), 'No QR was requested: not_registered'],
    ];
    for (const [request, line] of cases) {
      cleanup();
      api.listOwnerChannels.mockResolvedValue(withQrRequest(request));
      const view = render(StewardChannels);
      await waitFor(() => expect(feedback(view.container)).toBe(line));
      expect(newQr(view.container)!.disabled).toBe(false);
    }
  });

  it('"on its way" lasts only until the QR could have come: a QR that expired or an attempt that failed leaves the status line alone', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:30Z'));
    const done = qrRequest({status: 'done', completed_at: '2026-11-18T12:00:03Z', result: {requested: true}});
    api.listOwnerChannels.mockResolvedValue(withQrRequest(done, {status: {connected: false, detail: 'QR requested'}}));
    const view = render(StewardChannels);
    await waitFor(() => expect(feedback(view.container)).toBe('The QR is on its way: it shows up here in a few seconds.'));
    // A minute after the bridge took it no QR is on screen: it failed at once, or came and
    // expired. The detail line says which; "on its way" would be false.
    for (const detail of ['no QR (login_failed) - request one on the dashboard', 'no QR (qr_expired) - request one on the dashboard']) {
      api.listOwnerChannels.mockResolvedValue(withQrRequest(done, {status: {connected: false, detail}}));
      await vi.advanceTimersByTimeAsync(40_000);
      await waitFor(() => expect(card(view.container, 'wechat').textContent).toContain(detail));
      expect(feedback(view.container)).toBeUndefined();
      expect(newQr(view.container)!.disabled).toBe(false);
    }
  });

  it('a request that reads as expired while a QR is on screen says nothing (the host ran it but could not write back)', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:03:00Z'));
    api.listOwnerChannels.mockResolvedValue(withQrRequest(qrRequest({status: 'expired'}),
      {qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>'}));
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-qr] img')).not.toBeNull());
    expect(feedback(view.container)).toBeUndefined();
  });

  it('rate_limited says how many minutes to wait and keeps the button off until then', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:01:00Z'));
    api.listOwnerChannels.mockResolvedValue(withQrRequest(qrRequest({status: 'done', completed_at: '2026-11-18T12:00:00Z',
      result: {refused: 'rate_limited', retry_after_s: 300}})));
    const view = render(StewardChannels);
    await waitFor(() => expect(feedback(view.container)).toBe('Too many requests: try again in 4 min.'));
    expect(newQr(view.container)!.disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(4 * 60_000);
    await waitFor(() => expect(newQr(view.container)!.disabled).toBe(false));
  });

  it('a failed request shows the error and leaves the button usable', async () => {
    api.requestOwnerChannelQr.mockRejectedValue(new Error('403 owner_session_required'));
    const view = render(StewardChannels);
    await waitFor(() => expect(newQr(view.container)).not.toBeNull());
    await fireEvent.click(newQr(view.container)!);
    await waitFor(() => expect(view.container.querySelector('.error')?.textContent).toContain('owner_session_required'));
    expect(newQr(view.container)!.disabled).toBe(false);
  });
});

describe('the channels tab', () => {
  it('is one of the steward pane\'s tabs', async () => {
    api.dshDriverConversation.mockResolvedValue(page([]));
    const view = render(StewardPane, {projectId: 'aitelier-home', status: status('steward', 'aitelier-home'), onanswer: vi.fn()});
    await fireEvent.click(view.getByRole('tab', {name: 'Channels'}));
    await waitFor(() => expect(card(view.container, 'slack')).not.toBeNull());
    expect(api.listOwnerChannels).toHaveBeenCalled();
  });

  it('shows the bound account, "not bound", and the host\'s status with its detail', async () => {
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'slack')).not.toBeNull());
    const slack = card(view.container, 'slack'), wechat = card(view.container, 'wechat');
    expect(slack.dataset.bound).toBe('1');
    expect(slack.querySelector('[data-channel-account]')?.textContent).toContain('U0OWNER');
    expect(slack.querySelector('[data-channel-status]')?.textContent?.trim()).toBe('Connected');
    expect(within(slack).getByRole('button', {name: 'Unbind'})).toBeTruthy();
    expect(wechat.dataset.bound).toBe('0');
    expect(wechat.querySelector('[data-channel-account]')?.textContent?.trim()).toBe('Not bound');
    expect(wechat.querySelector('[data-channel-status]')?.getAttribute('data-channel-status')).toBe('disconnected');
    expect(wechat.querySelector('[data-channel-detail]')?.textContent).toBe('scan needed');
    expect(within(wechat).queryByRole('button', {name: 'Unbind'})).toBeNull();
  });

  it('a channel the host reported nothing for shows no status and is still listed', async () => {
    api.listOwnerChannels.mockResolvedValue({channels: [], default_channel: null});
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    expect(card(view.container, 'slack').querySelector('[data-channel-status]')).toBeNull();
    expect(card(view.container, 'slack').dataset.bound).toBe('0');
  });

  it('shows when the host reported the status, and a login QR as an image (never as markup) with the scan line', async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29"><path stroke="#000" d="M4 4.5h7"/></svg>';
    const listed = channels();
    listed.channels[1] = {...listed.channels[1], status: {connected: false, detail: 'scan needed', at: '2026-11-18T12:00:00Z'}, qr_svg: svg};
    api.listOwnerChannels.mockResolvedValue(listed);
    const view = render(StewardChannels);
    const qr = await waitFor(() => {
      const el = card(view.container, 'wechat').querySelector('[data-channel-qr]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(card(view.container, 'wechat').querySelector('[data-channel-at]')?.textContent)
      .toBe(new Date('2026-11-18T12:00:00Z').toLocaleString());
    const img = qr.querySelector('img') as HTMLImageElement;
    const src = img.getAttribute('src') ?? '';
    expect(src.startsWith('data:image/svg+xml;base64,')).toBe(true);
    expect(new TextDecoder().decode(Uint8Array.from(atob(src.slice(src.indexOf(',') + 1)), c => c.charCodeAt(0)))).toBe(svg);
    expect(img.getAttribute('alt')).toBe('Scan this with the WeChat account you want to bind to log it in');
    expect(qr.textContent).toContain('Scan this with the WeChat account you want to bind to log it in');
    expect(view.container.querySelector('svg')).toBeNull();
    expect(card(view.container, 'slack').querySelector('[data-channel-qr]')).toBeNull();
  });

  it('the scan line is in Chinese for a Chinese owner, and a hostile SVG never becomes markup', async () => {
    langStore.set('zh-CN');
    const hostile = '<svg xmlns="http://www.w3.org/2000/svg" onload="window.pwned=1"><script>window.pwned=1</script></svg>';
    const listed = channels();
    listed.channels[1] = {...listed.channels[1], qr_svg: hostile};
    api.listOwnerChannels.mockResolvedValue(listed);
    const view = render(StewardChannels);
    const qr = await waitFor(() => {
      const el = card(view.container, 'wechat').querySelector('[data-channel-qr]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(qr.textContent).toContain('用要绑定的微信小号扫码登录');
    // 7.7.13: the intro says what a bound family group may do, in the owner's language.
    expect(view.container.textContent).toContain('绑定的那几个家庭群（各有名字，每个渠道至多三个）只能问公开项目，不能改任何东西');
    expect(view.container.querySelector('script, svg, [onload]')).toBeNull();
    expect(view.container.innerHTML).not.toContain('<script');
    expect((window as unknown as {pwned?: number}).pwned).toBeUndefined();
  });

  it('while a login QR is shown the list is re-read, so a new QR or the login shows; then the re-reading stops', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    const scanning = channels();
    scanning.channels[1] = {...scanning.channels[1], qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>'};
    api.listOwnerChannels.mockResolvedValue(scanning);
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-qr]')).not.toBeNull());
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
    const loggedIn = channels();
    loggedIn.channels[1] = {...loggedIn.channels[1], status: {connected: true, at: '2026-11-18T12:00:20Z'}, qr_svg: null};
    api.listOwnerChannels.mockResolvedValue(loggedIn);
    await vi.advanceTimersByTimeAsync(6000);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-qr]')).toBeNull());
    expect(card(view.container, 'wechat').querySelector('[data-channel-status]')?.getAttribute('data-channel-status')).toBe('connected');
    const calls = api.listOwnerChannels.mock.calls.length;
    await vi.advanceTimersByTimeAsync(20000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(calls);
  });

  it('a login QR that first appears after the tab loaded shows on a slow re-read while a channel is not connected', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    expect(card(view.container, 'wechat').querySelector('[data-channel-qr]')).toBeNull();
    // Slower than the bind-code re-read: nothing at 10 s.
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
    const scanning = channels();
    scanning.channels[1] = {...scanning.channels[1], qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>'};
    api.listOwnerChannels.mockResolvedValue(scanning);
    await vi.advanceTimersByTimeAsync(6000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-qr] img')).not.toBeNull());
  });

  it('with every channel connected the list is not re-read', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    const connected = channels();
    connected.channels[1] = {...connected.channels[1], status: {connected: true}};
    api.listOwnerChannels.mockResolvedValue(connected);
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-status]')?.getAttribute('data-channel-status'))
      .toBe('connected'));
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
  });

  it('a hidden tab does not re-read slowly, and a destroyed view stops re-reading', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    try {
      const view = render(StewardChannels);
      await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
      await vi.advanceTimersByTimeAsync(60000);
      expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
      visibility.mockReturnValue('visible');
      await vi.advanceTimersByTimeAsync(1000);
      expect(api.listOwnerChannels).toHaveBeenCalledTimes(2);
      view.unmount();
      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(60000);
      expect(api.listOwnerChannels).toHaveBeenCalledTimes(2);
    } finally { visibility.mockRestore(); }
  });

  it('Bind shows the one-time code, the instruction and a countdown to its expiry', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    api.createOwnerChannelCode.mockResolvedValue({code: '482913', expires_at: '2026-11-18T12:10:00Z'});
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    await fireEvent.click(within(card(view.container, 'wechat')).getByRole('button', {name: 'Bind'}));
    await waitFor(() => expect(api.createOwnerChannelCode).toHaveBeenCalledWith('wechat'));
    const code = await waitFor(() => {
      const el = card(view.container, 'wechat').querySelector('[data-channel-code]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(code.querySelector('.digits')?.textContent).toBe('482913');
    expect(code.textContent).toContain('Send this code to the bot from the account you want to bind.');
    // Risk (j), 7.7.9: the code is for the 1:1 chat only - never a group.
    expect(code.querySelector('[data-code-no-group]')?.textContent).toBe('Send it only in a 1:1 chat with the bot; never paste it into a group.');
    expect(code.querySelector('[data-code-countdown]')?.textContent).toBe('Expires in 10:00');
    await vi.advanceTimersByTimeAsync(61000);
    expect(code.querySelector('[data-code-countdown]')?.textContent).toBe('Expires in 8:59');
    await vi.advanceTimersByTimeAsync(540000);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-code-expired]')).not.toBeNull());
    expect(card(view.container, 'wechat').querySelector('.digits')).toBeNull();
  });

  it('while a code is live the list is re-read, and the code goes away once the binding shows', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    api.createOwnerChannelCode.mockResolvedValue({code: '482913', expires_at: '2026-11-18T12:10:00Z'});
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
    // No code live: the clock ticks without the fast re-read (WeChat is not connected,
    // so the slow one would come at 15 s).
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(1);
    await fireEvent.click(within(card(view.container, 'wechat')).getByRole('button', {name: 'Bind'}));
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-code]')).not.toBeNull());
    const bound = channels();
    bound.channels[1] = {...bound.channels[1], bound: true, sender_ref: 'wxid_owner', bound_at: '2026-11-18T12:01:00Z'};
    api.listOwnerChannels.mockResolvedValue(bound);
    await vi.advanceTimersByTimeAsync(6000);
    await waitFor(() => expect(card(view.container, 'wechat').querySelector('[data-channel-code]')).toBeNull());
    expect(card(view.container, 'wechat').querySelector('[data-channel-account]')?.textContent).toContain('wxid_owner');
    expect(view.getByRole('status').textContent).toBe('WeChat is bound.');
  });

  it('Unbind asks first and is sent only on confirm', async () => {
    api.unbindOwnerChannel.mockResolvedValue({});
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'slack')).not.toBeNull());
    const slack = card(view.container, 'slack');
    await fireEvent.click(within(slack).getByRole('button', {name: 'Unbind'}));
    await frame();
    expect(api.unbindOwnerChannel).not.toHaveBeenCalled();
    const dialog = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    expect(dialog.textContent).toContain('U0OWNER');
    await fireEvent.click(within(dialog).getByRole('button', {name: 'No'}));
    expect(api.unbindOwnerChannel).not.toHaveBeenCalled();
    await fireEvent.click(within(slack).getByRole('button', {name: 'Unbind'}));
    await frame();
    const again = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    await fireEvent.click(within(again).getByRole('button', {name: 'Unbind'}));
    await waitFor(() => expect(api.unbindOwnerChannel).toHaveBeenCalledWith('slack'));
    expect(api.listOwnerChannels).toHaveBeenCalledTimes(2);
  });

  it('the default-channel selector PUTs the choice, and "none" as null', async () => {
    api.setDefaultOwnerChannel.mockResolvedValue({});
    const view = render(StewardChannels);
    const select = await waitFor(() => {
      const el = view.container.querySelector('.default select') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(select.value).toBe('slack');
    expect([...select.options].map(o => o.value)).toEqual(['', 'slack', 'wechat']);
    await fireEvent.change(select, {target: {value: 'wechat'}});
    await waitFor(() => expect(api.setDefaultOwnerChannel).toHaveBeenCalledWith('wechat'));
    await fireEvent.change(select, {target: {value: ''}});
    await waitFor(() => expect(api.setDefaultOwnerChannel).toHaveBeenLastCalledWith(null));
  });
});

// Step 25 C12 (design 7.9.2): each card shows its alert line (lib/channelAlert.ts, whose tables
// have their own tests), from the answer's server clock and the owner's zone.
describe('the channel-down alert line', () => {
  const NOW = '2026-11-23T11:00:00Z';
  // The backend's rules as GET /api/steward/channels answers them (alert_limits).
  const LIMITS = {after_minutes: 10, retry_after_minutes: 30, unreported_minutes: 10, attempts: 2, down_every_minutes: 360,
    quiet_from_hour: 23, quiet_until_hour: 8};
  const at = (minutes: number) => new Date(Date.parse(NOW) - minutes * 60_000).toISOString();
  const sent = {kind: 'down', down_since: at(30), state: 'sent', attempt: 1, via: 'slack', claimed_at: at(20), finished_at: at(20), error: null};
  const down = (wechatStatus: object, slack: object = {status: {connected: true, live: true}}, clock: object = {}) => ({
    ...channels({channels: [
      {channel: 'slack', bound: true, sender_ref: 'U0OWNER', bound_at: '2026-11-18T10:00:00Z', revoked_at: null, is_default: 1, ...slack},
      {channel: 'wechat', bound: true, sender_ref: 'wxid_owner', bound_at: '2026-11-18T10:00:00Z', revoked_at: null, is_default: 0,
        status: {connected: false, live: true, detail: 'no QR (logged_out)', down_since: at(30), up_since: null, alert_down: null,
          alert_up: null, last_told: null, last_down_sent_at: null, alert_due: null, ...wechatStatus}}]}),
    now: NOW, quiet_now: false, quiet_until: null, owner_tz: 'Asia/Shanghai', alert_limits: LIMITS, ...clock});
  const alertOf = (container: HTMLElement, channel: string) => card(container, channel).querySelector('[data-channel-alert]');

  it('says how long it is down and when (in the owner\'s zone) and where the owner was alerted', async () => {
    api.listOwnerChannels.mockResolvedValue(down({alert_down: sent}));
    const view = render(StewardChannels);
    await waitFor(() => expect(alertOf(view.container, 'wechat')).not.toBeNull());
    expect(alertOf(view.container, 'wechat')!.textContent).toBe('Down 30 min; alerted on Slack at 18:40');
    expect(alertOf(view.container, 'wechat')!.getAttribute('data-channel-alert')).toBe('alerted');
    expect(alertOf(view.container, 'slack')).toBeNull();
    langStore.set('zh-CN');
    await waitFor(() => expect(alertOf(view.container, 'wechat')!.textContent).toBe('断开 30 分钟，18:40 已在 Slack 告警'));
  });

  it('a given-up alert says it was not sent and why; a pending one says when it will go', async () => {
    api.listOwnerChannels.mockResolvedValue(down({alert_down: {...sent, attempt: 2, state: 'failed', error: '409 recipient_unknown'}}));
    const view = render(StewardChannels);
    await waitFor(() => expect(alertOf(view.container, 'wechat')).not.toBeNull());
    expect(alertOf(view.container, 'wechat')!.textContent).toBe('Down alert not sent: 409 recipient_unknown');
    cleanup();
    api.listOwnerChannels.mockResolvedValue(down({down_since: at(4)}));
    const early = render(StewardChannels);
    await waitFor(() => expect(alertOf(early.container, 'wechat')).not.toBeNull());
    expect(alertOf(early.container, 'wechat')!.textContent).toBe('Down 4 min; alert at 19:06 if still down');
  });

  it('no other connected channel says so', async () => {
    api.listOwnerChannels.mockResolvedValue(down({}, {status: {connected: false, live: true, detail: 'socket closed', down_since: at(1)}}));
    const view = render(StewardChannels);
    await waitFor(() => expect(alertOf(view.container, 'wechat')).not.toBeNull());
    expect(alertOf(view.container, 'wechat')!.textContent).toBe('No other channel to send the down alert through');
  });

  it('a connected channel with nothing owed, one not bound, or an answer without the server clock or rules has no line', async () => {
    api.listOwnerChannels.mockResolvedValue({...channels(), now: NOW, owner_tz: 'Europe/Paris', alert_limits: LIMITS});
    const view = render(StewardChannels);
    await waitFor(() => expect(card(view.container, 'wechat')).not.toBeNull());
    expect(view.container.querySelector('[data-channel-alert]')).toBeNull();
    for (const missing of [{now: undefined}, {alert_limits: undefined}]) {
      cleanup();
      api.listOwnerChannels.mockResolvedValue({...down({alert_down: sent}), ...missing});
      const old = render(StewardChannels);
      await waitFor(() => expect(card(old.container, 'wechat')).not.toBeNull());
      expect(old.container.querySelector('[data-channel-alert]')).toBeNull();
    }
  });

  it('the times follow the answer\'s rules, not a copy of them', async () => {
    api.listOwnerChannels.mockResolvedValue(down({down_since: at(4)}, undefined, {alert_limits: {...LIMITS, after_minutes: 20}}));
    const view = render(StewardChannels);
    await waitFor(() => expect(alertOf(view.container, 'wechat')).not.toBeNull());
    expect(alertOf(view.container, 'wechat')!.textContent).toBe('Down 4 min; alert at 19:16 if still down');
  });

  it('a plugin taken back is not connected: Disconnected, the new-QR button, and no real reading yet', async () => {
    // What the backend answers for "channel plugin stopped" (connected and live 0, no outage clock).
    api.listOwnerChannels.mockResolvedValue(down({connected: false, live: false, detail: 'channel plugin stopped', down_since: null,
      up_since: at(600)}));
    const view = render(StewardChannels);
    await waitFor(() => expect(alertOf(view.container, 'wechat')).not.toBeNull());
    const wechat = card(view.container, 'wechat');
    expect(wechat.querySelector('[data-channel-status]')?.getAttribute('data-channel-status')).toBe('disconnected');
    expect(wechat.querySelector('[data-channel-status]')?.textContent?.trim()).toBe('Disconnected');
    expect(wechat.querySelector('[data-channel-new-qr]')?.textContent?.trim()).toBe('New QR code');
    expect(alertOf(view.container, 'wechat')!.textContent).toBe('Real status not read yet (channel plugin stopped)');
  });
});

describe('message source badges', () => {
  it('mark channel messages in the steward conversation; no mark is the dashboard', async () => {
    api.dshDriverConversation.mockResolvedValue(page([entry('h4', 'driver'), entry('h3', 'driver'), entry('h2', 'user'), entry('h1', 'user')]));
    api.stewardMessageSources.mockResolvedValue({
      h2: {direction: 'in', channel: 'wechat', sender_ref: 'wxid_owner', channel_message_ref: 'w1', at: 'x', error: null},
      h3: {direction: 'out', channel: 'wechat', sender_ref: 'wxid_owner', channel_message_ref: 'w2', at: 'x', error: null},
      h4: {direction: 'out', channel: 'slack', sender_ref: 'U0OWNER', channel_message_ref: null, at: 'x', error: 'channel_not_found'},
    });
    const view = render(StewardPane, {projectId: 'aitelier-home', status: status('steward', 'aitelier-home'), onanswer: vi.fn()});
    const badge = (id: string) => view.container.querySelector(`[data-message-id="${id}"] [data-message-source]`) as HTMLElement | null;
    await waitFor(() => expect(badge('h2')).not.toBeNull());
    expect(sourceCalls()).toEqual([['h1', 'h2', 'h3', 'h4']]);
    expect(badge('h1')).toBeNull();
    expect(badge('h2')!.textContent).toBe('WeChat');
    expect(badge('h3')!.textContent).toBe('→ WeChat');
    expect(badge('h4')!.textContent).toBe('→ Slack ⚠');
    expect(badge('h4')!.classList.contains('failed')).toBe(true);
    expect(badge('h4')!.title).toBe('Not delivered to Slack: channel_not_found');
    langStore.set('zh-CN');
    await waitFor(() => expect(badge('h2')!.textContent).toBe('微信'));
  });

  it('a poll that brings nothing new asks nothing; a new message is asked alone', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval']});
    api.dshDriverConversation.mockResolvedValue(page([entry('h2', 'driver'), entry('h1', 'user')]));
    const view = render(StewardPane, {projectId: 'aitelier-home', status: status('steward', 'aitelier-home'), onanswer: vi.fn()});
    await waitFor(() => expect(sourceCalls()).toEqual([['h1', 'h2']]));
    await vi.advanceTimersByTimeAsync(10000);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(2));
    expect(sourceCalls()).toEqual([['h1', 'h2']]);
    api.dshDriverConversation.mockResolvedValue(page([entry('h3', 'user', '2026-11-18T10:05:00Z'), entry('h2', 'driver'), entry('h1', 'user')]));
    await vi.advanceTimersByTimeAsync(10000);
    await waitFor(() => expect(view.container.querySelector('[data-message-id="h3"]')).not.toBeNull());
    await waitFor(() => expect(sourceCalls()).toEqual([['h1', 'h2'], ['h3']]));
  });

  it('asks in batches of at most 200 ids', async () => {
    const many = Array.from({length: 250}, (_, i) => entry('m' + String(i).padStart(3, '0'), 'user',
      new Date(Date.UTC(2026, 10, 18, 0, 0, i)).toISOString()));
    api.dshDriverConversation.mockResolvedValue(page(many.slice().reverse()));
    render(StewardPane, {projectId: 'aitelier-home', status: status('steward', 'aitelier-home'), onanswer: vi.fn()});
    await waitFor(() => expect(sourceCalls().length).toBe(2));
    expect(sourceCalls().map(ids => ids.length)).toEqual([200, 50]);
  });

  it('a fresh steward reply with no row yet is asked once more, after its delivery could be recorded', async () => {
    vi.useFakeTimers({toFake: ['setInterval', 'clearInterval', 'setTimeout', 'Date']});
    vi.setSystemTime(new Date('2026-11-18T12:00:30Z'));
    api.dshDriverConversation.mockResolvedValue(page([entry('r2', 'driver', '2026-11-18T12:00:20Z'), entry('r1', 'user', '2026-11-18T12:00:00Z'),
      entry('old', 'driver', '2026-11-17T12:00:00Z')]));
    api.stewardMessageSources.mockResolvedValueOnce({});
    api.stewardMessageSources.mockResolvedValueOnce({
      r2: {direction: 'out', channel: 'slack', sender_ref: 'U0OWNER', channel_message_ref: '1.2', at: 'x', error: null}});
    const view = render(StewardPane, {projectId: 'aitelier-home', status: status('steward', 'aitelier-home'), onanswer: vi.fn()});
    await waitFor(() => expect(sourceCalls()).toEqual([['old', 'r1', 'r2']]));
    await vi.advanceTimersByTimeAsync(15000);
    // Only the fresh steward message: not the owner's (written with its row), not the old one.
    await waitFor(() => expect(sourceCalls()).toEqual([['old', 'r1', 'r2'], ['r2']]));
    await waitFor(() => expect(view.container.querySelector('[data-message-id="r2"] [data-message-source]')?.textContent).toBe('→ Slack'));
    await vi.advanceTimersByTimeAsync(60000);
    expect(sourceCalls().length).toBe(2);
  });

  it('a director\'s pane requests no sources', async () => {
    api.dshDriverConversation.mockResolvedValue(page([entry('d2', 'driver'), entry('d1', 'user')]));
    const view = render(DriverConversationPane, {projects: [projectRow('alpha')], status: status('director', 'alpha'), selected: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-message-id="d2"]')).not.toBeNull());
    await frame();
    expect(api.stewardMessageSources).not.toHaveBeenCalled();
    expect(view.container.querySelector('[data-message-source]')).toBeNull();
  });
});
