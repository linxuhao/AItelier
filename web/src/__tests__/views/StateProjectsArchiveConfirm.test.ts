/** Archiving a project from its card takes a deliberate second tap.
 *
 *  The owner asked for it in Chinese: "归档按钮加个二次确认吧，不小心点到就尴尬了"
 *  — one stray click archives a project and it disappears from every list. So the
 *  card's archive control STAGES instead of writing: the first tap reveals an
 *  inline confirmation naming the project and sends NOTHING, the confirming tap
 *  performs the same State write as before, and cancelling sends nothing and
 *  leaves the row exactly as it was. Restore is deliberately unchanged — one tap
 *  — because un-hiding a project is reversible and the owner asked for friction
 *  on the archive direction only.
 *
 *  Everything here is counted at the API boundary (the api module's
 *  `stateProjectArchive` is a spy), never read off rendered prose: "0 requests"
 *  is a claim about what left the page, and a rendered sentence cannot make it.
 *  The page's re-read is counted the same way, on `stateProjects`.
 *
 *  NOTE: the JS toolchain is not in the pytest image, so this file is measured on
 *  a host with `web/node_modules/.bin/vitest run --root web`, not in the
 *  container that runs the Python arm.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  driverStatus: vi.fn(), dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn(),
  messageDshDriver: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  listStewardProposals: vi.fn(),
  stateProjects: vi.fn(), stateProjectArchive: vi.fn(),
  stateOverview: vi.fn(), stateRunSummary: vi.fn(), stateNode: vi.fn(), stateAttempts: vi.fn(),
  stateDriverNote: vi.fn(), stateRefreshProject: vi.fn(), stateAttemptDetail: vi.fn(),
  runHistory: vi.fn(), listPipelines: vi.fn(), pipelineGraph: vi.fn(), pipelineStateFile: vi.fn(),
  getTrace: vi.fn(), setUserLang: vi.fn(),
  listRepos: vi.fn(), listAllRuns: vi.fn(), createProject: vi.fn(), deleteProject: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import StateProjects from '../../views/StateProjects.svelte';

const HERE = dirname(fileURLToPath(import.meta.url));

/** One catalog row. `archived` is State's own listing fact, and the page only
 *  RECEIVES it on a request that asked for archived rows. */
const row = (id: string, archived = false) => ({
  project_id: id, title: id, source_project_id: null, node_count: 1, verified_count: 1,
  source: { repo_path: null }, policy: { dispatch: 'active', revision: 0, reason: '' }, archived,
});
/** The catalog answer the SERVER gives: archived rows only exist in the answer to
 *  a request that asked for them (`include_archived`). */
function catalog(all: ReturnType<typeof row>[]) {
  api.stateProjects.mockImplementation(async (_repo?: string, _after = '', includeArchived = false) =>
    ({ projects: includeArchived ? all : all.filter(p => p.archived !== true), next_after: null }));
  return all;
}
/** Let any pending microtask/timer the page scheduled run, so "nothing was sent"
 *  is asserted after the page had every chance to send it. */
const settle = async (ms = 25) => { await new Promise(r => setTimeout(r, ms)); };
/** Every staged confirmation on screen, by the project id it names. */
const staged = (view: { container: HTMLElement }) =>
  [...view.container.querySelectorAll('[data-archive-confirm]')].map(e => e.getAttribute('data-archive-confirm'));
/** The catalog reads the page has issued so far (mount included). */
const reads = () => api.stateProjects.mock.calls.length;

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks();
  api.driverStatus.mockResolvedValue({ total: 0, online: 0, projects: {} });
  api.dshDriverConversationUnread.mockResolvedValue({ projects: {}, total: 0 });
  api.dshDriverConversation.mockResolvedValue({ project_id: 'live', messages: [], has_more: false, next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0, read_upto_message_id: null, read_upto_created_at: null });
  api.dshDriverHistory.mockResolvedValue({ driver_id: 'dsh', session_id: 's', entries: [], has_more: false, next_before_seq: null });
  api.sendDriverConversationMessage.mockResolvedValue({}); api.markDriverConversationRead.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({}); api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
  api.listStewardProposals.mockResolvedValue([]);
  api.stateProjectArchive.mockResolvedValue({ archived: true, changed: true });
  langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => storage.set(k, v) });
  authStore.set({ canWrite: true, permissionResolved: true, email: 'owner@local' });
  catalog([row('live'), row('paused')]);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('the archive control asks before it hides a project', () => {
  it('sends nothing on the first tap and reveals a confirmation naming that project', async () => {
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    const readsBefore = reads();
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    // the write did NOT leave the page, and the catalog was not re-read either:
    // the first tap is a question, not a decision
    expect(api.stateProjectArchive).not.toHaveBeenCalled();
    expect(reads()).toBe(readsBefore);
    // the question is on screen, inline, naming the project it would hide
    expect(staged(view)).toEqual(['live']);
    const confirm = view.container.querySelector('[data-archive-confirm="live"]')!;
    expect(confirm.textContent).toContain('live');
    expect(confirm.querySelector('[data-archive-confirm-name="live"]')!.textContent).toBe('live');
    // ...and it is a question about THIS row only: the other card asked nothing
    expect(view.container.querySelector('[data-archive-control="paused"]')).toBeTruthy();
    expect(staged(view)).toHaveLength(1);
  });

  it('performs exactly one archive write on the confirming tap, then re-reads the page', async () => {
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    const readsBefore = reads();
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    await fireEvent.click(view.getByRole('button', { name: 'Yes, archive live' }));
    await waitFor(() => expect(api.stateProjectArchive).toHaveBeenCalledTimes(1));
    // the write the existing control always made, for the project that was named
    expect(api.stateProjectArchive.mock.calls).toEqual([['live', true]]);
    // and the page re-read the catalog, as it does after every archive write
    await waitFor(() => expect(reads()).toBe(readsBefore + 1));
    // the confirmation is spent, not still sitting there armed
    expect(staged(view)).toEqual([]);
  });

  it('sends nothing when the confirmation is cancelled and leaves the row listed', async () => {
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    const readsBefore = reads();
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    await fireEvent.click(view.getByRole('button', { name: 'Cancel, keep it listed live' }));
    await settle();
    expect(api.stateProjectArchive).not.toHaveBeenCalled();
    expect(reads()).toBe(readsBefore);
    expect(staged(view)).toEqual([]);
    // the row is exactly where it was, with its archive control available again
    expect(view.getByRole('heading', { name: 'live' })).toBeTruthy();
    expect(view.getByRole('button', { name: 'Archive live' })).toBeTruthy();
  });

  it('stages only the row that was tapped: a confirmation cannot be confirmed for another project', async () => {
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    expect(staged(view)).toEqual(['live']);
    // tapping the OTHER project's control does not confirm the first one: it
    // replaces the question with its own, and still sends nothing
    await fireEvent.click(view.getByRole('button', { name: 'Archive paused' }));
    await settle();
    expect(api.stateProjectArchive).not.toHaveBeenCalled();
    expect(staged(view)).toEqual(['paused']);
    // and the confirming tap answers the question that is actually on screen
    await fireEvent.click(view.getByRole('button', { name: 'Yes, archive paused' }));
    await waitFor(() => expect(api.stateProjectArchive).toHaveBeenCalledTimes(1));
    expect(api.stateProjectArchive.mock.calls).toEqual([['paused', true]]);
  });

  it('drops a staged confirmation when the page re-reads the catalog', async () => {
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    expect(staged(view)).toEqual(['live']);
    // ask for archived rows: the page re-reads the catalog, and the row the
    // confirmation was staged on is replaced by the server's answer
    const readsBefore = reads();
    await fireEvent.click(view.getByLabelText(/Show archived/));
    await waitFor(() => expect(reads()).toBe(readsBefore + 1));
    expect(staged(view)).toEqual([]);
    // the row is still listed (nothing was written), but the question it carried
    // did not survive the re-read: it can only be answered against the rows that
    // were on screen when it was asked
    expect(api.stateProjectArchive).not.toHaveBeenCalled();
    expect(view.getByRole('heading', { name: 'live' })).toBeTruthy();
    await fireEvent.click(view.getByRole('button', { name: 'Archive live' }));
    await settle();
    expect(staged(view)).toEqual(['live']);
  });
});

describe('restoring an archived project stays one tap', () => {
  it('sends the unarchive write on the first tap and asks nothing', async () => {
    catalog([row('live'), row('old-canary', true)]);
    const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
    await fireEvent.click(view.getByLabelText(/Show archived/));
    await view.findByRole('heading', { name: 'old-canary' });
    const readsBefore = reads();
    await fireEvent.click(view.getByRole('button', { name: 'Restore old-canary' }));
    await waitFor(() => expect(api.stateProjectArchive).toHaveBeenCalledTimes(1));
    // un-hiding is reversible, so it is NOT staged: no confirmation ever appears
    expect(api.stateProjectArchive.mock.calls).toEqual([['old-canary', false]]);
    expect(staged(view)).toEqual([]);
    await waitFor(() => expect(reads()).toBe(readsBefore + 1));
  });
});

describe('the labels are translated, not written into the component', () => {
  it('renders the confirmation from the i18n table in each language', async () => {
    // [the card's own archive label, the question, confirm, cancel] — the three
    // blocks' own strings, so a block that lost a key falls back to English and
    // fails here instead of shipping a half-translated confirmation.
    const expected: Record<string, [string, string, string, string]> = {
      en: ['Archive', 'Archive this project?', 'Yes, archive', 'Cancel, keep it listed'],
      zh: ['归档', '确定要归档这个项目吗？', '确认归档', '取消，保持列出'],
      fr: ['Archiver', 'Archiver ce projet ?', 'Oui, archiver', 'Annuler, le garder listé'],
    };
    for (const [lang, [tap, ask, yes, no]] of Object.entries(expected)) {
      cleanup(); langStore.set(lang);
      const view = render(StateProjects); await view.findByRole('heading', { name: 'live' });
      await fireEvent.click(view.getByRole('button', { name: `${tap} live` }));
      await new Promise(r => setTimeout(r, 25));
      const confirm = view.container.querySelector('[data-archive-confirm="live"]')!;
      expect(confirm.textContent).toContain(ask);
      expect(view.getByRole('button', { name: yes + ' live' })).toBeTruthy();
      expect(view.getByRole('button', { name: no + ' live' })).toBeTruthy();
    }
    langStore.set('en');
  });

  it('keeps the same key set in the en, zh and fr blocks for the archive labels', () => {
    // Read from the module's own source: `st()` falls back to `en`, so a key
    // missing from ONE block renders English text in a translated page and no
    // rendered assertion would notice. The scan is string-aware on purpose - a
    // value may contain a comma and a colon without being a key.
    const source = readFileSync(resolve(HERE, '../../lib/stateI18n.svelte.ts'), 'utf8');
    const keysOf = (name: string) => {
      const start = source.indexOf(`const ${name}: Record<string, string> = {`);
      expect(start, `${name} block present`).toBeGreaterThan(-1);
      const end = source.indexOf('\n};\n', start);
      expect(end, `${name} block closed`).toBeGreaterThan(start);
      const body = source.slice(start, end);
      const keys = new Set<string>();
      let i = 0, quote = '';
      while (i < body.length) {
        const ch = body[i];
        if (quote) {
          if (ch === '\\') { i += 2; continue; }
          if (ch === quote) quote = '';
          i++; continue;
        }
        if (ch === "'" || ch === '"' || ch === '`') { quote = ch; i++; continue; }
        if (ch === ',' || ch === '{') {
          const m = /^[,{]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?=['"`])/.exec(body.slice(i));
          if (m) { keys.add(m[1]); i += m[0].length; continue; }
        }
        i++;
      }
      return keys;
    };
    const en = keysOf('en'), zh = keysOf('zh'), fr = keysOf('fr');
    // every label these archive controls render, the three new ones included
    const family = ['showArchived', 'archivedHidden', 'archiveProject', 'restoreProject',
      'archivedBadge', 'archiveDismiss', 'archiveConfirm', 'archiveConfirmYes', 'archiveConfirmCancel'];
    for (const key of family) {
      expect([key, en.has(key), zh.has(key), fr.has(key)]).toEqual([key, true, true, true]);
    }
    const ofFamily = (keys: Set<string>) => family.filter(k => keys.has(k)).sort();
    expect(ofFamily(zh)).toEqual(ofFamily(en));
    expect(ofFamily(fr)).toEqual(ofFamily(en));
  });
});
