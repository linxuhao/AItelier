/** The project list shows how many questions the user still owes an answer to.
 *
 *  The number is not recomputed here: the server sends it (per project) from
 *  `core/driver_questions.py`, and the conversation's questions box decides
 *  "open" with `openQuestions`. The first describe below is what keeps those two
 *  in agreement - it reads the SAME fixture file the Python test reads, so a
 *  change to either rule that the other does not follow shows up as a mismatch
 *  before the badge can disagree with the box.
 *
 *  The second describe is the badge itself: a non-zero count renders its own
 *  pill beside the unread one, and a zero renders NOTHING - an empty badge would
 *  read as "you have questions" on every project.
 *
 *  NOTE: the JS toolchain is not in the pytest image, so this file is measured on
 *  a host with `npm test`, not in the container that runs the Python arm.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
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
import { openQuestions } from '../../lib/questions';
import type { DriverConversationEntry } from '../../lib/api';

// Resolved from THIS file, not from the cwd: `vitest` is run from web/.
// (`new URL(..., import.meta.url)` is rewritten into a served asset URL by Vite,
// which is not a file: URL, so the path is built explicitly instead.)
const HERE = dirname(fileURLToPath(import.meta.url));
const FAMILY_PATH = resolve(HERE, '../../../../tests/fixtures/driver_questions_open_family.json');
const FAMILY = JSON.parse(readFileSync(FAMILY_PATH, 'utf8')) as {
  open_count: number; rows: DriverConversationEntry[]};

describe('the shared fixture family keeps the web rule and the server rule equal', () => {
  it('openQuestions yields exactly the fixture\'s open_count', () => {
    // Same file as tests/unit/test_driver_questions_open_count.py. If the server
    // grew a second definition of "open", this number stops matching the badge
    // the server sends, and the box stops matching the list.
    expect(openQuestions(FAMILY.rows)).toHaveLength(FAMILY.open_count);
    // Named, so a rule that gets the right COUNT for the wrong reasons fails.
    expect(openQuestions(FAMILY.rows).map(q => [q.message_id, q.segment_id]))
      .toEqual([['m1', 'q2'], ['m6', 'q5']]);
  });
});

// ------------------------------------------------------------------ the badge
const row = (id: string) => ({
  project_id: id, title: id, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: '2026-01-01T00:00:00+00:00',
});
const managed = (phase: string) =>
  ({enabled: 1, phase, driver_id: 'dsh-' + phase, last_error: '',
    updated_at: '2026-06-01T00:00:00+00:00'});
const table = () => ({total: 2, online: 2, projects: {
  hot: {total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
    managed: managed('running')},
  cold: {total: 1, online: 1, running_subagents: 0, running_pipelines: 0,
    managed: managed('running')},
}});
const clone = (value: unknown) => JSON.parse(JSON.stringify(value));

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot'), row('cold')], next_after: null});
  api.driverStatus.mockImplementation(async () => clone(table()));
  api.dshDriverConversationUnread.mockResolvedValue(
    {projects: {}, total: 0, questions: {}, questions_total: 0});
  api.dshDriverConversation.mockResolvedValue({project_id: 'hot', messages: [], has_more: false,
    next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0,
    read_upto_message_id: null, read_upto_created_at: null});
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-running', session_id: 's',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const cards = (container: HTMLElement) => [...container.querySelectorAll('.project-card')] as HTMLElement[];
const card = (container: HTMLElement, id: string) =>
  cards(container).find(c => c.querySelector('h2')?.textContent === id)!;

describe('a project with open questions says so on its own card', () => {
  it('renders the question count as its own badge beside the unread badge', async () => {
    api.dshDriverConversationUnread.mockResolvedValue({
      projects: {hot: 3}, total: 3, questions: {hot: 2}, questions_total: 2});
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    await waitFor(() => expect(
      card(view.container, 'hot').querySelector('[data-pending-questions]')).not.toBeNull());

    const badge = card(view.container, 'hot').querySelector('[data-pending-questions]') as HTMLElement;
    expect(badge.textContent?.trim()).toBe('2');
    expect(badge.getAttribute('title')).toBe('unanswered questions');
    expect(badge.classList.contains('questions')).toBe(true);
    // Two badges, not one merged number: unread says 3, questions says 2.
    expect(card(view.container, 'hot').querySelector('.unread')?.textContent).toBe('3');
    expect(badge.textContent).not.toBe('3');
    // The project with no open question carries no question badge at all.
    expect(card(view.container, 'cold').querySelector('[data-pending-questions]')).toBeNull();
  });

  it('renders nothing when the project has no open question', async () => {
    // The server omits a project with zero open questions; a total of 0 is the
    // same state seen from the summary.
    api.dshDriverConversationUnread.mockResolvedValue(
      {projects: {hot: 1}, total: 1, questions: {}, questions_total: 0});
    const view = render(StateProjects);
    await view.findByRole('heading', {name: 'hot'});
    await waitFor(() => expect(card(view.container, 'hot').querySelector('.unread')).not.toBeNull());
    expect(view.container.querySelector('[data-pending-questions]')).toBeNull();
    // The unread badge is untouched by the new one's absence.
    expect(card(view.container, 'hot').querySelector('.unread')?.textContent).toBe('1');
  });
});
