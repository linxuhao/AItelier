import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor, within } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

// The dashboard's Run history tab (views/DriverRunLog.svelte). Its header used
// to show "Close history" unconditionally, but the pane that hosts it always
// shows the run log and passes no `onclose`, so there was nothing to dismiss:
// pressing the button closed the live stream, never reconnected it, and left
// the tab reading "Reconnecting…" with no way back except Refresh. These three
// tests pin the fix from the RENDERED DOM — never from the source text:
//
//   1. the control exists only where a host actually handles the dismissal;
//   2. paging back may pause the live appends, but the tab must not end up
//      stranded on an indicator it cannot leave, and Refresh must return to the
//      newest page on a new stream that still delivers records;
//   3. where no live stream exists at all, Refresh is the one reload — it
//      re-fetches the page and shows what arrived since.

const api = vi.hoisted(() => ({
  stateProjects: vi.fn(), driverStatus: vi.fn(),
  dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(), messageDshDriver: vi.fn(),
  startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import DriverRunLog from '../../views/DriverRunLog.svelte';
import StateProjects from '../../views/StateProjects.svelte';

// A stream the test controls: it records every connection, whether the
// component closed it, and the test decides when it opens or delivers events.
type Stream = { url: string; onopen: (() => void) | null; onerror: (() => void) | null;
  onmessage: ((event: {data: string}) => void) | null; closed: boolean; close(): void };
let streams: Stream[] = [];
class FakeEventSource {
  onopen: (() => void) | null = null; onerror: (() => void) | null = null;
  onmessage: ((event: {data: string}) => void) | null = null; closed = false;
  constructor(public url: string) { streams.push(this as unknown as Stream); }
  close() { this.closed = true; }
}

const row = (id: string) => ({
  project_id: id, title: id, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: '2026-01-01T00:00:00+00:00',
});
// A history page as the component reads it: `has_more` is what puts the
// "Older records" control in the tab, and `next_before_seq` is the cursor the
// older page is asked for.
const record = (seq: number, text: string) => ({seq, type: 'tool/result', text});
const historyPage = (entries: any[], has_more = false) => ({
  driver_id: 'dsh-hot', session_id: 's', entries,
  has_more, next_before_seq: entries[0]?.seq ?? null,
});
const conversationPage = {project_id: 'hot', messages: [], has_more: false, next_cursor: null,
  end_of_history: true, matched_total: 0, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null};

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map(); streams = [];
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot'), row('cold')], next_after: null});
  // `hot` has a managed driver, so the pane offers its Run history tab with a
  // live driver id; `cold` has none.
  api.driverStatus.mockResolvedValue({total: 1, online: 1, projects: {
    hot: {total: 1, online: 1, running_subagents: 1, running_pipelines: 0,
      managed: {enabled: 1, phase: 'running', driver_id: 'dsh-hot', last_error: '',
        updated_at: '2026-06-01T00:00:00+00:00'}},
  }});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue(conversationPage);
  api.dshDriverHistory.mockResolvedValue(historyPage([]));
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('dismissal-only-when-a-host-handles-it', () => {
  it('renders no Close history control without an onclose handler, and one that calls it when given', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const bare = render(DriverRunLog, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await bare.findByText('No persisted records yet');
    // Absent from the DOM, not merely hidden: role queries skip hidden nodes by
    // default, so the hidden flag is asked for explicitly, and the raw text is
    // asked for as well in case the control lost its role.
    expect(bare.queryByRole('button', {name: 'Close history', hidden: true})).toBeNull();
    expect(bare.queryByText('Close history')).toBeNull();
    // Refresh is the control that stays: with no stream it is the path to new
    // records, so removing the dead button must not remove it too.
    expect(bare.getByRole('button', {name: 'Refresh'})).toBeTruthy();
    cleanup();

    const onclose = vi.fn();
    const hosted = render(DriverRunLog, {projectId: 'alpha', driverId: 'dsh-alpha', onclose});
    await hosted.findByText('No persisted records yet');
    await fireEvent.click(hosted.getByRole('button', {name: 'Close history'}));
    expect(onclose).toHaveBeenCalledTimes(1);
  });
});

describe('paging-may-pause-live-but-never-strands-the-tab', () => {
  it('goes quiet on the older page and comes back live on a new stream from Refresh', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const newest = historyPage([record(20, 'newest record')], true);
    const older = historyPage([record(10, 'older record')]);
    // The cursor decides which page the host is asking for: no cursor means the
    // newest page, a seq means the page before it.
    api.dshDriverHistory.mockImplementation(async (_id: string, before?: number) =>
      before === undefined ? newest : older);

    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    const card = [...view.container.querySelectorAll('.project-card')]
      .find(c => (c.textContent ?? '').includes('hot')) as HTMLElement;
    await fireEvent.click(card);
    // The run log mounts only once the pane knows the driver, so wait for the
    // selection and the driver toggle before switching to the tab.
    await waitFor(() => {
      const link = view.container.querySelector('aside.conversation-pane .pane-project') as HTMLAnchorElement;
      expect(link?.getAttribute('href') ?? '').toContain('hot');
    });
    await view.findByRole('button', {name: 'Stop driver'});
    await fireEvent.click(view.getByRole('tab', {name: 'Run history'}));
    const panel = view.container.querySelector('[data-mode-panel="run"]') as HTMLElement;
    const run = within(panel);

    // The tab opened exactly one stream for this driver, and it is live once the
    // server accepts the connection.
    await waitFor(() => expect(streams.length).toBe(1));
    const first = streams[0];
    expect(first.url).toContain(encodeURIComponent('dsh:dsh-hot'));
    first.onopen!();
    await run.findByText('Live');
    await run.findByText('newest record');

    // Paging back is allowed to pause the appends, but a tab that has been
    // paged must not claim to be live and must not sit on the reconnect
    // indicator: neither state is true while it is showing an older page.
    await fireEvent.click(await run.findByRole('button', {name: 'Older records'}));
    await run.findByText('older record');
    expect(run.queryByText('Live')).toBeNull();
    expect(run.queryByText('Reconnecting…')).toBeNull();

    // Refresh is the way back: the newest page again, on a NEW stream, and that
    // stream still delivers records into the tab.
    await fireEvent.click(run.getByRole('button', {name: 'Refresh'}));
    await waitFor(() => expect(streams.length).toBe(2));
    const replacement = streams[1];
    expect(replacement).not.toBe(first);
    await run.findByText('newest record');
    expect(run.queryByText('older record')).toBeNull();
    replacement.onopen!();
    await run.findByText('Live');
    expect(run.queryByText('Reconnecting…')).toBeNull();
    replacement.onmessage!({data: JSON.stringify({log: JSON.stringify({
      type: 'driver_activity', phase: 'running', subagents: [], entry: record(30, 'delivered on the new stream'),
    })})});
    await run.findByText('delivered on the new stream');
  });
});

describe('refresh-stays-where-it-is-the-only-reload', () => {
  it('reloads the page on Refresh with no live stream, and shows a record that arrived since', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    api.dshDriverHistory
      .mockResolvedValueOnce(historyPage([record(1, 'first record')]))
      .mockResolvedValue(historyPage([record(2, 'second record'), record(1, 'first record')]));
    // driverEnabled is left at its default (false): no live stream may be opened.
    const view = render(DriverRunLog, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('first record');
    expect(api.dshDriverHistory).toHaveBeenCalledTimes(1);
    expect(view.queryByText('second record')).toBeNull();

    await fireEvent.click(view.getByRole('button', {name: 'Refresh'}));
    await view.findByText('second record');
    expect(api.dshDriverHistory).toHaveBeenCalledTimes(2);
    expect(api.dshDriverHistory).toHaveBeenLastCalledWith('alpha', undefined);
    expect(streams).toHaveLength(0);
  });
});
