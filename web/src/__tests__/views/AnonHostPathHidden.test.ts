/**
 * The dashboard must not hand anonymous visitors the SERVER's filesystem.
 *
 * Both the project card (StateProjects.svelte) and the project detail source
 * block (StateProject.svelte) used to render `source.repo_path` unconditionally,
 * so a logged-out browser was shown `/home/<user>/.AItelier/projects/<dir>` —
 * the host's layout, its user name and its directory convention, which say
 * nothing about the project and everything about the machine. The path is a
 * writer-only fact now: a writer ($authStore.canWrite) still reads the binding,
 * an anonymous reader gets no source row at all — not the path, and not a
 * "no source repository bound" placeholder either, which would still speak
 * about a binding the reader may not see.
 *
 * Both branches are asserted, on both surfaces, after the store flips: this is
 * a guard that must react, not a value that happens to be null.
 */
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { overview, detail, runSummary } from '../fixtures/stateProject';

const api = vi.hoisted(() => ({
  driverStatus: vi.fn(), dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn(),
  messageDshDriver: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  stateProjects: vi.fn(), stateOverview: vi.fn(), stateRunSummary: vi.fn(), stateNode: vi.fn(),
  stateAttempts: vi.fn(), stateDriverNote: vi.fn(), stateRefreshProject: vi.fn(), stateAttemptDetail: vi.fn(),
  setUserLang: vi.fn(),
}));
vi.mock('../../lib/api', () => api);

import StateProjects from '../../views/StateProjects.svelte';
import StateProject from '../../views/StateProject.svelte';

const HOST_PATH = '/home/somebody/.AItelier/projects/demo-repo';
// Any absolute POSIX path that starts at a real host root. The point is not
// this exact string: it is that no host-shaped path appears anywhere in the
// rendered text.
const HOST_SHAPED = /(^|\s)\/(home|Users|srv|opt|app|var)\//;
const PROJECT_KEY = 'iss-0237';
const PROJECT_TITLE = 'Demo repo project';
const NO_SOURCE = 'No source repository bound';

const anonymous = { canWrite: false, permissionResolved: true, email: null };
const writer = { canWrite: true, permissionResolved: true, email: 'owner@local' };

function row() {
  return {
    project_id: PROJECT_KEY, title: PROJECT_TITLE, source_project_id: null,
    node_count: 2, verified_count: 1,
    source: { repo_path: HOST_PATH }, policy: { dispatch: 'active', revision: 0, reason: '' },
  };
}

function boundOverview() {
  const base = overview();
  return {
    ...base,
    project: { project_id: PROJECT_KEY, title: PROJECT_TITLE, source_project_id: null },
    source: { kind: 'stable_binding', repo_path: HOST_PATH, common_dir: HOST_PATH + '/.git', revision: 1 },
  };
}

let storage: Map<string, string>;

beforeEach(() => {
  cleanup(); vi.resetAllMocks();
  api.driverStatus.mockResolvedValue({ total: 0, online: 0, projects: {} });
  api.dshDriverConversationUnread.mockResolvedValue({ projects: {}, total: 0 });
  api.dshDriverConversation.mockResolvedValue({ project_id: PROJECT_KEY, messages: [], has_more: false,
    next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0,
    read_upto_message_id: null, read_upto_created_at: null });
  api.dshDriverHistory.mockResolvedValue({ driver_id: 'dsh', session_id: 's', entries: [], has_more: false, next_before_seq: null });
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.markDriverConversationRead.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({}); api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
  api.stateProjects.mockResolvedValue({ projects: [row()], next_after: null });
  api.stateOverview.mockResolvedValue(boundOverview());
  api.stateRunSummary.mockImplementation(async (id: string) => ({ ...runSummary(), project_id: id }));
  api.stateNode.mockImplementation(async (_p: string, key: string) => detail(key));
  api.stateAttempts.mockResolvedValue({ attempts: [], next_after: null });
  api.stateDriverNote.mockResolvedValue({ project_id: PROJECT_KEY, revision: 1, index: [], entry_count: 0, listed_count: 0, delisted_count: 0 });
  langStore.set('en');
  storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('the project card keeps the host path for writers only', () => {
  it('shows neither the path nor a placeholder to an anonymous visitor, then shows the path to a writer', async () => {
    authStore.set({ ...anonymous });
    const view = render(StateProjects);
    await view.findByRole('heading', { name: PROJECT_TITLE });
    await tick();
    // Not an empty render: the card itself is there.
    const text = view.container.textContent ?? '';
    expect(text).toContain(PROJECT_KEY);
    expect(text).toContain(PROJECT_TITLE);
    // The measurement: no host-shaped absolute path anywhere.
    expect(text).not.toMatch(HOST_SHAPED);
    expect(text).not.toContain(HOST_PATH);
    // ...and no "no source repository bound" stand-in either.
    expect(text).not.toContain(NO_SOURCE);
    expect(view.container.querySelector('.source')).toBeNull();

    // The writer's branch, on the same mounted view: the guard reacts.
    authStore.set({ ...writer });
    await waitFor(() => expect(view.container.textContent).toContain(HOST_PATH));
    expect(view.container.textContent).not.toContain(NO_SOURCE);
  });
});

describe('the project detail source block keeps the host path for writers only', () => {
  it('shows neither the source block nor a placeholder to an anonymous visitor, then shows the path to a writer', async () => {
    authStore.set({ ...anonymous });
    const view = render(StateProject, { params: { id: PROJECT_KEY } });
    await view.findByRole('heading', { name: PROJECT_TITLE });
    await tick();
    const text = view.container.textContent ?? '';
    expect(text).toContain(PROJECT_KEY);   // breadcrumb
    expect(text).toContain(PROJECT_TITLE); // header
    expect(text).not.toMatch(HOST_SHAPED);
    expect(text).not.toContain(HOST_PATH);
    expect(text).not.toContain(NO_SOURCE);
    expect(view.container.querySelector('details.source')).toBeNull();

    authStore.set({ ...writer });
    await waitFor(() => expect(view.container.textContent).toContain(HOST_PATH));
    expect(view.container.querySelector('details.source')).not.toBeNull();
    expect(view.container.textContent).not.toContain(NO_SOURCE);
  });
});
