/** The owner's steward on the dashboard (plugin step 14, design section 6).
 *
 *  - the steward card is pinned first and `aitelier-home` is not an ordinary project;
 *  - the questions summary adds up the same per-project numbers the badges show,
 *    and lists each project's questions through the shared rule (`openQuestions`);
 *  - a proposal card follows open -> executing -> terminal, and a 409 refreshes it;
 *  - a stop_driver card with work in flight needs the explicit "stop anyway";
 *  - the profile page edits against a version and shows the text that won a conflict;
 *  - purge asks first; the old butler's /chat route is gone;
 *  - reminders (step 16): the list marks steward edits and disabled rows, the form
 *    previews the backend's next runs, saves with the browser zone / the version it
 *    read, shows the row that won a 409, and deletes only after a confirm.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, within } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  stateProjects: vi.fn(), driverStatus: vi.fn(),
  dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(), messageDshDriver: vi.fn(),
  startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  listStewardProposals: vi.fn(), approveStewardProposal: vi.fn(), declineStewardProposal: vi.fn(),
  stewardProposalImpact: vi.fn(), createStewardMemoryRequest: vi.fn(), stewardMemoryRequest: vi.fn(),
  listStewardSchedules: vi.fn(), createStewardSchedule: vi.fn(), updateStewardSchedule: vi.fn(),
  deleteStewardSchedule: vi.fn(), previewStewardSchedule: vi.fn(),
}));
// The real module underneath, so ApiError is the class the components check.
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import { ApiError, type DriverConversationEntry, type StewardProposal, type StewardSchedule } from '../../lib/api';
import { openQuestions } from '../../lib/questions';
import { runMemoryRequest, totalOpenQuestions } from '../../lib/steward';
import StateProjects from '../../views/StateProjects.svelte';
import ProjectDashboard from '../../views/ProjectDashboard.svelte';
import StewardQuestions from '../../views/StewardQuestions.svelte';
import StewardProposalCard from '../../views/StewardProposalCard.svelte';
import StewardProfile from '../../views/StewardProfile.svelte';
import StewardSchedules from '../../views/StewardSchedules.svelte';
import StewardScheduleForm from '../../views/StewardScheduleForm.svelte';
import AppBar from '../../views/AppBar.svelte';
import { navArea } from '../../lib/navigation.svelte';

const row = (id: string, title = id) => ({
  project_id: id, title, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: '2026-01-01T00:00:00+00:00',
});
const activity = (phase: string, role?: string) => ({total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
  managed: {enabled: 1, phase, driver_id: 'dsh-' + phase, last_error: '', model: '',
    updated_at: '2026-06-01T00:00:00+00:00', ...(role ? {role} : {})}});
const table = () => ({total: 3, online: 3, projects: {
  'aitelier-home': activity('waiting', 'steward'),
  alpha: activity('running', 'director'),
  beta: activity('running'),          // no role: a director
}});
const page = (messages: DriverConversationEntry[] = []) => ({project_id: 'x', messages, has_more: false,
  next_cursor: null, end_of_history: true, matched_total: messages.length, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null});
const asked = (project: string, id: string, segments: string[], at: string): DriverConversationEntry => ({
  message_id: id, thread_id: 't', project_id: project, sender_kind: 'driver', sender_driver_id: 'd',
  subject: 's', body: '', refs: {}, reply_to_message_id: null, created_at: at,
  segments: segments.map(s => ({id: s, kind: 'question' as const, text: 'question ' + s})),
});
const answer = (project: string, id: string, cites: [string, string][], at: string): DriverConversationEntry => ({
  message_id: id, thread_id: 't', project_id: project, sender_kind: 'user', sender_driver_id: null,
  subject: '', body: 'ok', refs: {}, reply_to_message_id: null, created_at: at,
  answers: cites.map(([message_id, segment_id]) => ({message_id, segment_id})),
});
// alpha: three questions asked, one answered -> 2 open; beta: one open.
const conversations: Record<string, DriverConversationEntry[]> = {
  alpha: [asked('alpha', 'a1', ['q1', 'q2'], '2026-01-01T00:00:01Z'), asked('alpha', 'a2', ['q3'], '2026-01-01T00:00:02Z'),
    answer('alpha', 'a3', [['a1', 'q1']], '2026-01-01T00:00:03Z')],
  beta: [asked('beta', 'b1', ['q1'], '2026-01-01T00:00:01Z')],
};
const proposal = (over: Partial<StewardProposal> = {}): StewardProposal => ({
  proposal_id: 'p1', version: 3, kind: 'start_driver', payload: {project_id: 'alpha'}, reason: 'it is idle',
  status: 'open', created_at: '2026-11-17T00:00:00Z', decided_at: null, decided_by: null, result: null, ...over,
});

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.stateProjects.mockResolvedValue({projects: [row('alpha'), row('aitelier-home', 'Home'), row('beta')], next_after: null});
  api.driverStatus.mockImplementation(async () => table());
  api.dshDriverConversationUnread.mockResolvedValue(
    {projects: {}, total: 0, questions: {alpha: 2, beta: 1}, questions_total: 3});
  api.dshDriverConversation.mockImplementation(async (id: string) => page(conversations[id] ?? []));
  api.dshDriverHistory.mockResolvedValue({driver_id: 'd', session_id: 's', entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.listStewardProposals.mockImplementation(async (status: string) =>
    status === 'open' ? [proposal(), proposal({proposal_id: 'p2'})] : [proposal()]);
  api.stewardProposalImpact.mockResolvedValue({running_subagents: [], unsettled_attempts: []});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const ordinaryCards = (container: HTMLElement) =>
  [...container.querySelectorAll('.project-grid .project-card')] as HTMLElement[];

describe('the steward card', () => {
  it('is pinned before every ordinary project and counts busy drivers, questions and proposals', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'alpha'});
    const pinned = await waitFor(() => {
      const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    // First: no ordinary card precedes it in document order.
    const first = ordinaryCards(view.container)[0];
    expect(pinned.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const all = [...view.container.querySelectorAll('.project-card')];
    expect(all[0]).toBe(pinned);
    await waitFor(() => {
      expect(pinned.querySelector('[data-steward-fact="busy"]')?.textContent).toBe('2');
      expect(pinned.querySelector('[data-steward-fact="questions"]')?.textContent).toBe('3');
      expect(pinned.querySelector('[data-steward-fact="proposals"]')?.textContent).toBe('2');
    });
    expect(api.listStewardProposals).toHaveBeenCalledWith('open', 100);
  });

  it('keeps aitelier-home out of the ordinary cards and the project picker', async () => {
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-steward-card]')).not.toBeNull());
    const titles = ordinaryCards(view.container).map(c => c.querySelector('h2')?.textContent);
    expect(titles).toEqual(expect.arrayContaining(['alpha', 'beta']));
    expect(titles).not.toContain('Home');
    const options = [...view.container.querySelectorAll('.mobile-project-controls option')].map(o => (o as HTMLOptionElement).value);
    expect(options).not.toContain('aitelier-home');
  });

  it('is hidden by role too, not only by the reserved id', async () => {
    api.stateProjects.mockResolvedValue({projects: [row('alpha'), row('house', 'House')], next_after: null});
    api.driverStatus.mockResolvedValue({total: 2, online: 2, projects: {house: activity('waiting', 'steward'), alpha: activity('waiting')}});
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-steward-card]')).not.toBeNull());
    expect(ordinaryCards(view.container).map(c => c.querySelector('h2')?.textContent)).toEqual(['alpha']);
  });

  it('keeps aitelier-home out of the dashboard project list', async () => {
    const view = render(ProjectDashboard);
    const select = await waitFor(() => {
      const el = view.container.querySelector('.project-switcher select') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    const values = [...select.options].map(o => o.value);
    expect(values).toEqual(['alpha', 'beta']);
  });

  it('opens the steward pane: the aitelier-home conversation with its own tabs', async () => {
    const view = render(StateProjects);
    const pinned = await waitFor(() => {
      const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.click(pinned);
    const pane = await view.findByRole('complementary');
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('aitelier-home'));
    for (const name of ['Proposals', 'Open questions by project', 'Reminders', 'Profile']) {
      expect(within(pane).getByRole('tab', {name})).toBeTruthy();
    }
  });

  it('shows the receipt under the owner\'s unanswered message, from the steward\'s phase', async () => {
    conversations['aitelier-home'] = [answer('aitelier-home', 'h1', [], '2026-01-01T00:00:01Z')];
    try {
      const view = render(StateProjects);
      const pinned = await waitFor(() => {
        const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
        expect(el).not.toBeNull();
        return el;
      });
      await fireEvent.click(pinned);
      const pane = await view.findByRole('complementary');
      // The table above has the steward 'waiting': delivered, about to be picked up.
      const ack = await waitFor(() => {
        const el = pane.querySelector('[data-conversation-ack]');
        expect(el).not.toBeNull();
        return el as HTMLElement;
      });
      expect(ack.dataset.conversationAck).toBe('waiting');
      expect(ack.textContent).toBe('Delivered - it will pick this up in a moment…');
    } finally { delete conversations['aitelier-home']; }
  });
});

describe('the open-questions summary', () => {
  it('adds up the same per-project numbers the badges show, each from the shared rule', async () => {
    const badges = {alpha: 2, beta: 1};
    // The rule itself agrees with the badges for these conversations.
    expect(openQuestions(conversations.alpha)).toHaveLength(badges.alpha);
    expect(openQuestions(conversations.beta)).toHaveLength(badges.beta);
    const onanswer = vi.fn();
    const view = render(StewardQuestions, {questions: badges, projects: [row('alpha', 'Alpha'), row('beta', 'Beta')] as never, onanswer});
    await waitFor(() => {
      expect(view.container.querySelector('[data-summary-project="alpha"] [data-summary-count]')?.textContent).toBe('2');
      expect(view.container.querySelector('[data-summary-project="beta"] [data-summary-count]')?.textContent).toBe('1');
      expect(view.container.querySelectorAll('[data-summary-project="alpha"] li')).toHaveLength(2);
    });
    const counts = [...view.container.querySelectorAll('[data-summary-count]')].map(el => Number(el.textContent));
    const total = Number(view.container.querySelector('[data-summary-total]')?.textContent);
    expect(total).toBe(counts.reduce((a, b) => a + b, 0));
    expect(total).toBe(totalOpenQuestions(badges));
    // Each group carries its project tag and a way to go and answer there.
    const alpha = view.container.querySelector('[data-summary-project="alpha"]') as HTMLElement;
    expect(within(alpha).getByText('Alpha')).toBeTruthy();
    await fireEvent.click(within(alpha).getByRole('button', {name: 'Go answer'}));
    expect(onanswer).toHaveBeenCalledWith('alpha');
  });

  it('agrees with the steward card total and the per-project badges on the project list', async () => {
    const view = render(StateProjects);
    const pinned = await waitFor(() => {
      const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
      expect(el?.querySelector('[data-steward-fact="questions"]')?.textContent).toBe('3');
      return el;
    });
    const perCard = [...view.container.querySelectorAll('[data-pending-questions]')]
      .map(el => Number(el.getAttribute('data-pending-questions')));
    expect(perCard.reduce((a, b) => a + b, 0)).toBe(3);
    await fireEvent.click(pinned);
    await fireEvent.click(await view.findByRole('tab', {name: 'Open questions by project'}));
    await waitFor(() => expect(view.container.querySelector('[data-summary-total]')?.textContent).toBe('3'));
  });

  it('"Go answer" opens that project\'s conversation', async () => {
    const view = render(StateProjects);
    const pinned = await waitFor(() => {
      const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.click(pinned);
    await fireEvent.click(await view.findByRole('tab', {name: 'Open questions by project'}));
    const beta = await waitFor(() => {
      const el = view.container.querySelector('[data-summary-project="beta"]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.click(within(beta).getByRole('button', {name: 'Go answer'}));
    await waitFor(() => expect(view.container.querySelector('[data-steward-card]')?.getAttribute('aria-pressed')).toBe('false'));
    expect(view.container.querySelector('.pane-project-id')?.textContent).toContain('beta');
  });
});

describe('a proposal card', () => {
  const props = (p: StewardProposal) => ({proposal: p, onupdate: vi.fn(), onreload: vi.fn(), onrevise: vi.fn()});
  const buttons = (container: HTMLElement) => [...container.querySelectorAll('.actions button')] as HTMLButtonElement[];

  it('open: Approve, Decline and Change it are enabled, and Approve sends the version shown', async () => {
    const p = props(proposal());
    api.approveStewardProposal.mockResolvedValue(proposal({status: 'executing', version: 4}));
    const view = render(StewardProposalCard, p);
    expect(buttons(view.container).map(b => [b.textContent, b.disabled])).toEqual(
      [['Approve', false], ['Decline', false], ['Change it', false]]);
    await fireEvent.click(view.getByRole('button', {name: 'Approve'}));
    await waitFor(() => expect(p.onupdate).toHaveBeenCalledWith(expect.objectContaining({status: 'executing'})));
    expect(api.approveStewardProposal).toHaveBeenCalledWith('p1', 3, false);
  });

  it('decline sends the version shown', async () => {
    const p = props(proposal());
    api.declineStewardProposal.mockResolvedValue(proposal({status: 'declined'}));
    const view = render(StewardProposalCard, p);
    await fireEvent.click(view.getByRole('button', {name: 'Decline'}));
    await waitFor(() => expect(p.onupdate).toHaveBeenCalled());
    expect(api.declineStewardProposal).toHaveBeenCalledWith('p1', 3);
  });

  it('create_project: lists the seed nodes in the shape the backend takes (key: goal)', () => {
    const view = render(StewardProposalCard, props(proposal({kind: 'create_project',
      payload: {project_id: 'ledger', title: 'Ledger', goal: 'a small ledger', seed_nodes: [{key: 'n1', goal: 'list needs', acceptance: 'a list'}]}})));
    expect(view.container.textContent).toContain('n1: list needs');
  });

  it('executing: a spinner and no buttons', () => {
    const view = render(StewardProposalCard, props(proposal({status: 'executing'})));
    expect(view.container.querySelector('[aria-busy="true"]')?.textContent).toBe('Executing…');
    expect(view.container.querySelectorAll('button')).toHaveLength(0);
  });

  it.each(['done', 'failed', 'declined', 'withdrawn', 'superseded'] as const)('%s: the result and no buttons', (status) => {
    const view = render(StewardProposalCard, props(proposal({status, decided_by: 'owner:me', result: {error: 'project_id taken'}})));
    expect(view.container.querySelectorAll('button')).toHaveLength(0);
    expect(view.container.querySelector('[data-result]')?.textContent).toContain('project_id taken');
  });

  it('409: takes the current row from the refusal and says it changed', async () => {
    const p = props(proposal());
    const current = proposal({version: 5, reason: 'updated'});
    api.approveStewardProposal.mockRejectedValue(new ApiError(409, '', null, current));
    const view = render(StewardProposalCard, p);
    await fireEvent.click(view.getByRole('button', {name: 'Approve'}));
    await view.findByText('This proposal changed while you were looking; the current version is shown.');
    expect(p.onupdate).toHaveBeenCalledWith(current);
    expect(p.onreload).not.toHaveBeenCalled();
  });

  it('409 without a row: re-reads the list', async () => {
    const p = props(proposal());
    api.declineStewardProposal.mockRejectedValue(new ApiError(409, 'conflict'));
    const view = render(StewardProposalCard, p);
    await fireEvent.click(view.getByRole('button', {name: 'Decline'}));
    await view.findByText('This proposal changed while you were looking; the current version is shown.');
    expect(p.onreload).toHaveBeenCalled();
  });

  it('a 409 carries the row through the real error parser', async () => {
    const current = proposal({version: 9});
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({detail: current}), {status: 409})));
    const {approveStewardProposal} = await vi.importActual<typeof import('../../lib/api')>('../../lib/api');
    const error = await approveStewardProposal('p1', 3).catch(e => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.detail).toEqual(current);
  });

  it('"Change it" hands the row to the host', async () => {
    const p = props(proposal());
    const view = render(StewardProposalCard, p);
    await fireEvent.click(view.getByRole('button', {name: 'Change it'}));
    expect(p.onrevise).toHaveBeenCalledWith(expect.objectContaining({proposal_id: 'p1'}));
  });
});

describe('a stop_driver card', () => {
  const stop = proposal({kind: 'stop_driver', payload: {project_id: 'alpha',
    impact: {running_subagents: [{session_id: 'sess-old'}], unsettled_attempts: []}}});
  const props = () => ({proposal: stop, onupdate: vi.fn(), onreload: vi.fn(), onrevise: vi.fn()});

  it('shows what stopping interrupts and holds Approve until "stop anyway" is ticked', async () => {
    api.stewardProposalImpact.mockResolvedValue({running_subagents: [{session_id: 'sess-now'}], unsettled_attempts: [{attempt_id: 'att-1'}]});
    api.approveStewardProposal.mockResolvedValue({...stop, status: 'executing'});
    const view = render(StewardProposalCard, props());
    expect(view.getByText('sess-old')).toBeTruthy();       // recorded when proposed
    await view.findByText('sess-now');                      // read again now
    expect(view.getByText('att-1')).toBeTruthy();
    const approve = view.getByRole('button', {name: 'Approve'}) as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    await fireEvent.click(view.getByLabelText('Stop anyway (interrupts the work listed above)'));
    expect(approve.disabled).toBe(false);
    await fireEvent.click(approve);
    await waitFor(() => expect(api.approveStewardProposal).toHaveBeenCalledWith('p1', 3, true));
  });

  it('needs no tick when nothing is running now', async () => {
    api.approveStewardProposal.mockResolvedValue({...stop, status: 'executing'});
    const view = render(StewardProposalCard, props());
    const approve = view.getByRole('button', {name: 'Approve'}) as HTMLButtonElement;
    await waitFor(() => expect(approve.disabled).toBe(false));
    expect(view.queryByLabelText('Stop anyway (interrupts the work listed above)')).toBeNull();
    await fireEvent.click(approve);
    await waitFor(() => expect(api.approveStewardProposal).toHaveBeenCalledWith('p1', 3, false));
  });

  it('422 at the click: shows the impact the server just measured and asks for the tick', async () => {
    // The card measured "nothing running" when it rendered, and work started before
    // the click. The approve endpoint measures again and refuses until the owner
    // confirms; the refusal carries that measurement, so the card must show it and
    // render the tick instead of an opaque status text the owner cannot act on.
    api.approveStewardProposal
      .mockRejectedValueOnce(new ApiError(422, 'Unprocessable Entity', null, {
        code: 'confirm_interrupt_required',
        impact: {running_subagents: [{session_id: 'sess-late'}], unsettled_attempts: []},
        proposal: stop}))
      .mockResolvedValueOnce({...stop, status: 'executing'});
    const view = render(StewardProposalCard, props());
    const approve = view.getByRole('button', {name: 'Approve'}) as HTMLButtonElement;
    await waitFor(() => expect(approve.disabled).toBe(false));
    await fireEvent.click(approve);
    await view.findByText('sess-late');
    expect(api.approveStewardProposal.mock.calls[0]).toEqual(['p1', 3, false]);
    expect(view.getByRole('status').textContent).toContain('Something started since this card loaded');
    expect(approve.disabled).toBe(true);
    await fireEvent.click(view.getByLabelText('Stop anyway (interrupts the work listed above)'));
    await fireEvent.click(approve);
    await waitFor(() => expect(api.approveStewardProposal).toHaveBeenLastCalledWith('p1', 3, true));
  });
});

describe('"change it" goes through the conversation', () => {
  it('prefills the steward composer with the proposal id and focuses it', async () => {
    const view = render(StateProjects);
    const pinned = await waitFor(() => {
      const el = view.container.querySelector('[data-steward-card]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.click(pinned);
    await fireEvent.click(await view.findByRole('tab', {name: 'Proposals'}));
    await fireEvent.click(await view.findByRole('button', {name: 'Change it'}));
    const composer = view.container.querySelector('textarea') as HTMLTextAreaElement;
    await waitFor(() => expect(composer.value).toBe('About proposal p1: '));
    expect(view.getByRole('tab', {name: 'Conversation'}).getAttribute('aria-selected')).toBe('true');
    await waitFor(() => expect(document.activeElement).toBe(composer));
    expect(api.sendDriverConversationMessage).not.toHaveBeenCalled();
  });
});

// ConfirmDialog focuses its first button on the next animation frame; let that
// frame run while the dialog is still mounted.
const frame = () => new Promise(done => requestAnimationFrame(() => done(null)));

describe('the profile page', () => {
  // Each request answers at once; the polling path is tested on runMemoryRequest below.
  function memory(answer: (kind: string, payload: Record<string, unknown>) => Record<string, unknown>) {
    api.createStewardMemoryRequest.mockImplementation(async (kind: string, payload: Record<string, unknown>) =>
      ({request_id: 'r-' + kind, status: 'done', result: answer(kind, payload)}));
  }

  it('edits against the version it read and shows the text that won a conflict', async () => {
    memory((kind) => kind === 'profile_read' ? {text: 'likes tea', version: 'v1', limit: 4000}
      : kind === 'forgotten_list' ? {forgotten: []}
      : {refused: 'version_conflict', current: {text: 'likes coffee', version: 'v2'}});
    const view = render(StewardProfile);
    await view.findByText('likes tea');
    expect(view.container.querySelector('[data-profile-version]')?.textContent).toBe('v1');
    await fireEvent.click(view.getByRole('button', {name: 'Edit'}));
    const editor = view.container.querySelector('[data-profile-editor]') as HTMLTextAreaElement;
    await fireEvent.input(editor, {target: {value: 'likes green tea'}});
    await fireEvent.input(view.getByLabelText('Why you are changing it'), {target: {value: 'more precise'}});
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(api.createStewardMemoryRequest).toHaveBeenCalledWith('profile_update',
      {text: 'likes green tea', expected_version: 'v1', reason: 'more precise'}));
    expect((await view.findByText('likes coffee')).hasAttribute('data-profile-current')).toBe(true);
    // Starting from the current text re-bases the edit on its version.
    await fireEvent.click(view.getByRole('button', {name: 'Start from this version'}));
    expect(editor.value).toBe('likes coffee');
    memory(() => ({updated: true, version: 'v3'}));
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(api.createStewardMemoryRequest).toHaveBeenLastCalledWith('profile_update',
      {text: 'likes coffee', expected_version: 'v2', reason: 'more precise'}));
    await view.findByText('Saved.');
  });

  it('purge asks for confirmation, saying it cannot be undone, before it is sent', async () => {
    memory((kind) => kind === 'profile_read' ? {text: '', version: 'v1', limit: 4000}
      : kind === 'forgotten_list' ? {forgotten: [{id: 'e1', at: '2026-11-17', by: 'owner'}]}
      : {purged: true, ids: ['e1']});
    const view = render(StewardProfile);
    const entry = await waitFor(() => {
      const el = view.container.querySelector('[data-forgotten="e1"]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.click(within(entry).getByRole('button', {name: 'Purge'}));
    await frame();
    expect(api.createStewardMemoryRequest).not.toHaveBeenCalledWith('purge', expect.anything());
    const dialog = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    expect(dialog.textContent).toContain('This cannot be undone');
    // Cancelling sends nothing.
    await fireEvent.click(within(dialog).getByRole('button', {name: 'No'}));
    expect(api.createStewardMemoryRequest).not.toHaveBeenCalledWith('purge', expect.anything());
    await fireEvent.click(within(entry).getByRole('button', {name: 'Purge'}));
    await frame();
    const again = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    await fireEvent.click(within(again).getByRole('button', {name: 'Purge'}));
    await waitFor(() => expect(api.createStewardMemoryRequest).toHaveBeenCalledWith('purge', {ids: ['e1']}));
    await view.findByText('Purged.');
    expect(view.container.querySelector('[data-forgotten="e1"]')).toBeNull();
  });
});

describe('reminders', () => {
  const schedule = (over: Partial<StewardSchedule> = {}): StewardSchedule => ({
    schedule_id: 'sch-1', version: 4, title: 'Morning check', instruction: 'look at the open questions',
    schedule: {kind: 'cron', expr: '0 9 * * 1-5'}, tz: 'Europe/Paris', enabled: true,
    next_run_at: '2026-11-18T08:00:00+00:00', last_run_at: '2026-11-17T08:00:00+00:00', last_missed: 0,
    created_by: 'owner', updated_by: 'owner', last_fired_by: null,
    created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-17T08:00:00Z', ...over,
  });
  const runs = ['2026-11-18T09:00:00+01:00', '2026-11-19T09:00:00+01:00', '2026-11-20T09:00:00+01:00',
    '2026-11-23T09:00:00+01:00', '2026-11-24T09:00:00+01:00'];
  const card = (container: HTMLElement, id: string) => container.querySelector(`[data-schedule="${id}"]`) as HTMLElement;
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const form = () => ({onchange: vi.fn(), onclose: vi.fn()});
  beforeEach(() => { api.previewStewardSchedule.mockResolvedValue(runs); });

  it('review 337: a reminder past its time by more than five minutes is marked overdue (a stopped clock shows)', async () => {
    api.listStewardSchedules.mockResolvedValue([
      schedule({overdue_seconds: 1800}),
      schedule({schedule_id: 'sch-2', title: 'Just due', overdue_seconds: 40}),
      schedule({schedule_id: 'sch-3', title: 'Off', enabled: false, overdue_seconds: 0}),
    ]);
    const view = render(StewardSchedules);
    await view.findByText('Morning check');
    expect(card(view.container, 'sch-1').querySelector('[data-overdue]')?.textContent).toBe('Overdue by 30 min');
    expect(card(view.container, 'sch-2').querySelector('[data-overdue]')).toBeNull();
    expect(card(view.container, 'sch-3').querySelector('[data-overdue]')).toBeNull();
  });

  it('lists each reminder with its schedule, runs, author, the steward-edit mark and disabled rows', async () => {
    api.listStewardSchedules.mockResolvedValue([
      schedule({updated_by: 'steward'}),
      schedule({schedule_id: 'sch-2', title: 'Pay rent', schedule: {kind: 'once', at: '2026-12-01T08:00:00Z'},
        enabled: false, next_run_at: null, last_run_at: null, created_by: 'steward', updated_by: 'owner'}),
    ]);
    const view = render(StewardSchedules);
    await view.findByText('Morning check');
    const cron = card(view.container, 'sch-1'), once = card(view.container, 'sch-2');
    expect(cron.querySelector('[data-steward-edited]')?.textContent).toBe('Changed by the steward');
    expect(once.querySelector('[data-steward-edited]')).toBeNull();
    const when = cron.querySelector('[data-schedule-when]')?.textContent ?? '';
    expect(when).toContain('Weekdays at 9:00');
    expect(when).toContain('0 9 * * 1-5');
    expect(when).toContain('Next run');
    expect(cron.querySelector('[data-next]')?.textContent).not.toBe('—');
    expect(cron.querySelector('[data-last]')?.textContent).not.toBe('—');
    expect(cron.querySelector('[data-created-by]')?.textContent).toBe('You');
    expect((cron.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(true);
    // A disabled once-reminder: the time it was set for, no next run, switch off.
    expect(once.dataset.enabled).toBe('0');
    expect(once.classList.contains('disabled')).toBe(true);
    expect(once.querySelector('[data-schedule-when]')?.textContent).toContain('Once at');
    expect(once.querySelector('[data-next]')?.textContent).toBe('—');
    expect(once.querySelector('[data-last]')?.textContent).toBe('—');
    expect(once.querySelector('[data-created-by]')?.textContent).toBe('Steward');
    expect((once.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(false);
  });

  it('a preset fills the expression, and the preview shows the next 5 runs (debounced)', async () => {
    const view = render(StewardScheduleForm, form());
    const expr = view.container.querySelector('[data-schedule-expr]') as HTMLInputElement;
    await fireEvent.input(expr, {target: {value: '0'}});
    await fireEvent.input(expr, {target: {value: '0 9'}});
    await fireEvent.click(view.getByRole('button', {name: 'Weekdays at 9:00'}));
    expect(expr.value).toBe('0 9 * * 1-5');
    await waitFor(() => expect(view.container.querySelectorAll('[data-preview] li')).toHaveLength(5));
    // Only the expression the owner settled on was sent, with the zone shown on the form.
    expect(api.previewStewardSchedule).toHaveBeenCalledTimes(1);
    expect(api.previewStewardSchedule).toHaveBeenCalledWith({kind: 'cron', expr: '0 9 * * 1-5'}, browserZone);
    expect(view.container.querySelector('[data-schedule-tz]')?.textContent).toBe(browserZone);
  });

  it.each([
    ['too_frequent', 'Runs less than 15 minutes apart'],
    ['invalid_cron', 'Invalid cron expression'],
  ])('the preview shows a 422 %s inline with its reason', async (code, label) => {
    api.previewStewardSchedule.mockRejectedValue(new ApiError(422, '', null, {code, reason: 'from the backend'}));
    const view = render(StewardScheduleForm, form());
    await fireEvent.input(view.container.querySelector('[data-schedule-expr]') as HTMLInputElement, {target: {value: '* * * * *'}});
    const shown = await waitFor(() => {
      const el = view.container.querySelector('[data-preview-error]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(shown.textContent).toBe(`${label}: from the backend`);
    expect(view.container.querySelector('[data-preview]')).toBeNull();
  });

  it('create posts title, instruction, schedule and the browser zone', async () => {
    const p = form();
    api.createStewardSchedule.mockImplementation(async (fields: object) => schedule({...fields, schedule_id: 'sch-9', version: 1}));
    const view = render(StewardScheduleForm, p);
    await fireEvent.input(view.getByLabelText('Title'), {target: {value: ' Morning check '}});
    await fireEvent.input(view.getByLabelText('Instruction for the steward'), {target: {value: 'look at the open questions'}});
    await fireEvent.click(view.getByRole('button', {name: 'Every day at 9:00'}));
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(api.createStewardSchedule).toHaveBeenCalledWith({title: 'Morning check',
      instruction: 'look at the open questions', schedule: {kind: 'cron', expr: '0 9 * * *'}, tz: browserZone}));
    await waitFor(() => expect(p.onclose).toHaveBeenCalled());
    expect(p.onchange).toHaveBeenCalledWith(expect.objectContaining({schedule_id: 'sch-9'}));
  });

  it('a once reminder sends the datetime-local time (browser zone) as an ISO instant', async () => {
    api.createStewardSchedule.mockResolvedValue(schedule());
    const view = render(StewardScheduleForm, form());
    await fireEvent.input(view.getByLabelText('Title'), {target: {value: 'Pay rent'}});
    await fireEvent.input(view.getByLabelText('Instruction for the steward'), {target: {value: 'remind me'}});
    await fireEvent.click(view.getByLabelText('Once'));
    await fireEvent.input(view.container.querySelector('[data-schedule-at]') as HTMLInputElement, {target: {value: '2030-01-02T09:30'}});
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    const at = new Date('2030-01-02T09:30').toISOString();
    await waitFor(() => expect(api.createStewardSchedule).toHaveBeenCalledWith(
      {title: 'Pay rent', instruction: 'remind me', schedule: {kind: 'once', at}, tz: browserZone}));
    await waitFor(() => expect(api.previewStewardSchedule).toHaveBeenCalledWith({kind: 'once', at}, browserZone));
  });

  it('edit sends the version it read; a 409 shows the current row and the owner starts from it', async () => {
    api.listStewardSchedules.mockResolvedValue([schedule()]);
    const current = schedule({version: 6, title: 'Morning check (steward)', instruction: 'and the proposals',
      schedule: {kind: 'cron', expr: '0 9 * * 1'}, updated_by: 'steward'});
    api.updateStewardSchedule.mockRejectedValueOnce(new ApiError(409, '', null, {code: 'version_conflict', schedule: current}));
    const view = render(StewardSchedules);
    await view.findByText('Morning check');
    await fireEvent.click(within(card(view.container, 'sch-1')).getByRole('button', {name: 'Edit'}));
    const title = view.getByLabelText('Title') as HTMLInputElement;
    expect(title.value).toBe('Morning check');
    expect(view.container.querySelector('[data-schedule-tz]')?.textContent).toBe('Europe/Paris');
    await fireEvent.input(title, {target: {value: 'Morning review'}});
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(api.updateStewardSchedule).toHaveBeenCalledWith('sch-1', 4, {title: 'Morning review',
      instruction: 'look at the open questions', schedule: {kind: 'cron', expr: '0 9 * * 1-5'}, tz: 'Europe/Paris'}));
    const shown = await waitFor(() => {
      const el = view.container.querySelector('[data-schedule-current]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(shown.textContent).toContain('Morning check (steward)');
    expect(shown.textContent).toContain('0 9 * * 1');
    // The list behind the form shows the row that won as well.
    expect(card(view.container, 'sch-1').querySelector('[data-steward-edited]')).not.toBeNull();
    await fireEvent.click(view.getByRole('button', {name: 'Start from this version'}));
    expect(title.value).toBe('Morning check (steward)');
    expect((view.container.querySelector('[data-schedule-expr]') as HTMLInputElement).value).toBe('0 9 * * 1');
    api.updateStewardSchedule.mockResolvedValueOnce(schedule({version: 7}));
    await fireEvent.click(view.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(api.updateStewardSchedule).toHaveBeenLastCalledWith('sch-1', 6, expect.objectContaining(
      {title: 'Morning check (steward)', schedule: {kind: 'cron', expr: '0 9 * * 1'}})));
    await waitFor(() => expect(view.container.querySelector('form')).toBeNull());
  });

  it('delete asks first and sends the version shown', async () => {
    api.listStewardSchedules.mockResolvedValue([schedule()]);
    api.deleteStewardSchedule.mockResolvedValue({deleted: true});
    const view = render(StewardSchedules);
    await view.findByText('Morning check');
    const row = card(view.container, 'sch-1');
    await fireEvent.click(within(row).getByRole('button', {name: 'Delete'}));
    await frame();
    expect(api.deleteStewardSchedule).not.toHaveBeenCalled();
    const dialog = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    expect(dialog.textContent).toContain('Morning check');
    await fireEvent.click(within(dialog).getByRole('button', {name: 'No'}));
    expect(api.deleteStewardSchedule).not.toHaveBeenCalled();
    await fireEvent.click(within(row).getByRole('button', {name: 'Delete'}));
    await frame();
    const again = view.container.ownerDocument.querySelector('dialog[open]') as HTMLElement;
    await fireEvent.click(within(again).getByRole('button', {name: 'Delete'}));
    await waitFor(() => expect(api.deleteStewardSchedule).toHaveBeenCalledWith('sch-1', 4));
    await waitFor(() => expect(card(view.container, 'sch-1')).toBeNull());
  });

  it('the enabled switch sends a PUT with the version shown; a 409 shows the current row', async () => {
    api.listStewardSchedules.mockResolvedValue([schedule()]);
    api.updateStewardSchedule.mockResolvedValueOnce(schedule({version: 5, enabled: false, next_run_at: null}));
    const view = render(StewardSchedules);
    await view.findByText('Morning check');
    await fireEvent.click(card(view.container, 'sch-1').querySelector('[role="switch"]') as HTMLInputElement);
    await waitFor(() => expect(api.updateStewardSchedule).toHaveBeenCalledWith('sch-1', 4, {enabled: false}));
    await waitFor(() => expect(card(view.container, 'sch-1').dataset.enabled).toBe('0'));
    // Switching back while the steward changed it: the refusal's row is shown, the switch follows it.
    api.updateStewardSchedule.mockRejectedValueOnce(new ApiError(409, '', null,
      {code: 'version_conflict', schedule: schedule({version: 8, enabled: false, next_run_at: null, updated_by: 'steward'})}));
    await fireEvent.click(card(view.container, 'sch-1').querySelector('[role="switch"]') as HTMLInputElement);
    await waitFor(() => expect(api.updateStewardSchedule).toHaveBeenLastCalledWith('sch-1', 5, {enabled: true}));
    await view.findByText('This reminder changed meanwhile; the current version is shown.');
    const row = card(view.container, 'sch-1');
    expect((row.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(false);
    expect(row.querySelector('[data-steward-edited]')).not.toBeNull();
  });

  it('PUT carries its JSON body and a 409 its current row through the real client', async () => {
    const current = schedule({version: 9});
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({detail: {code: 'version_conflict', schedule: current}}), {status: 409}));
    vi.stubGlobal('fetch', fetchMock);
    const real = await vi.importActual<typeof import('../../lib/api')>('../../lib/api');
    const error = await real.updateStewardSchedule('sch-1', 4, {enabled: false}).catch(e => e);
    expect(fetchMock).toHaveBeenCalledWith('/api/steward/schedules/sch-1', expect.objectContaining(
      {method: 'PUT', body: JSON.stringify({expected_version: 4, enabled: false})}));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.detail).toEqual({code: 'version_conflict', schedule: current});
    await real.deleteStewardSchedule('sch-1', 4).catch(() => null);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/steward/schedules/sch-1?expected_version=4', expect.objectContaining({method: 'DELETE'}));
  });
});

describe('runMemoryRequest', () => {
  it('polls a pending request until the steward completes it', async () => {
    api.createStewardMemoryRequest.mockResolvedValue({request_id: 'r1', status: 'pending'});
    api.stewardMemoryRequest.mockResolvedValueOnce({request_id: 'r1', status: 'pending'})
      .mockResolvedValueOnce({request_id: 'r1', status: 'done', result: {text: 'x', version: 'v', limit: 4000}});
    const result = await runMemoryRequest('profile_read', {}, {intervalMs: 1, timeoutMs: 1000});
    expect(result).toEqual({text: 'x', version: 'v', limit: 4000});
    expect(api.createStewardMemoryRequest).toHaveBeenCalledWith('profile_read', {});
    expect(api.stewardMemoryRequest).toHaveBeenCalledWith('r1');
  });

  it('gives up at the bound and reports the steward offline (null)', async () => {
    api.createStewardMemoryRequest.mockResolvedValue({request_id: 'r1', status: 'pending'});
    api.stewardMemoryRequest.mockResolvedValue({request_id: 'r1', status: 'pending'});
    expect(await runMemoryRequest('profile_read', {}, {intervalMs: 5, timeoutMs: 30})).toBeNull();
  });
});

describe('the old butler is retired', () => {
  it('has no /chat route and no nav entry pointing at it', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const app = readFileSync(resolve(here, '../../App.svelte'), 'utf8');
    expect(app).not.toMatch(/['"]\/chat['"]/);
    expect(app).not.toMatch(/Chat\.svelte/);
    const view = render(AppBar);
    expect(view.container.querySelector('a[href="#/chat"]')).toBeNull();
    expect(view.container.querySelector('[data-nav="chat"]')).toBeNull();
    expect(navArea('#/chat')).toBe('');
  });
});
