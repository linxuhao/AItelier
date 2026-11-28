/** The steward's Proposals tab: a decision section beside a folded history,
 *  and a card whose title, content and Approve row are three separate boxes.
 *
 *  jsdom has no layout engine, so "the button is on screen" cannot be measured.
 *  What IS measurable is the box chain that makes it possible: a bounded card
 *  column, two independently scrolling children, and a non-shrinking decision
 *  row that is not a descendant of either scroller. The computed-style checks
 *  read the CSS Svelte actually injected (with its scoped class), so a rule
 *  dropped by the compiler or removed from the component fails the test.
 */
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
import { t } from '../../lib/i18n.svelte';

const api = vi.hoisted(() => ({
  listStewardProposals: vi.fn(),
  approveStewardProposal: vi.fn(), declineStewardProposal: vi.fn(),
  stewardProposalImpact: vi.fn(),
}));
// Keep the real module underneath so ApiError is the class the card checks.
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { StewardProposal } from '../../lib/api';
import StewardProposals from '../../views/StewardProposals.svelte';

const at = (n: number) => `2026-11-20T00:00:0${n}Z`;
const proposal = (id: string, status: StewardProposal['status'], when: number): StewardProposal => ({
  proposal_id: id, version: 1, kind: 'start_driver', payload: {project_id: 'alpha'},
  reason: 'a reason', status, created_at: at(when), decided_at: null, decided_by: null, result: null,
});
const props = () => ({onchange: vi.fn(), onrevise: vi.fn(), onconnect: vi.fn()});
const css = (el: Element | null, prop: string) =>
  el ? getComputedStyle(el as HTMLElement).getPropertyValue(prop).trim() : '<missing>';
const overflowY = (el: Element | null) => css(el, 'overflow-y') || css(el, 'overflow');
const ids = (section: Element) =>
  [...section.querySelectorAll('[data-proposal]')].map(el => el.getAttribute('data-proposal'));

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  api.listStewardProposals.mockResolvedValue([]);
  api.stewardProposalImpact.mockResolvedValue({running_subagents: [], unsettled_attempts: []});
});
afterEach(() => { cleanup(); langStore.set('en'); });

async function mount(rows: StewardProposal[]) {
  api.listStewardProposals.mockResolvedValue(rows);
  const view = render(StewardProposals, props());
  await waitFor(() => expect(view.container.querySelector('[data-proposal-section="active"]')).toBeTruthy());
  return {
    view,
    active: () => view.container.querySelector('[data-proposal-section="active"]') as HTMLElement,
    past: () => view.container.querySelector('[data-proposal-section="past"]') as HTMLElement,
    toggle: () => view.container.querySelector('[data-past-toggle]') as HTMLButtonElement,
  };
}

describe('the proposals tab separates what needs a decision from what is decided', () => {
  it('lists open and executing in the active section, folds every terminal status until the toggle is clicked', async () => {
    const {active, past, toggle} = await mount([
      proposal('older-open', 'open', 1), proposal('done-old', 'done', 2),
      proposal('newer-exec', 'executing', 9), proposal('failed-row', 'failed', 3),
      proposal('declined-row', 'declined', 4), proposal('withdrawn-row', 'withdrawn', 5),
      proposal('superseded-row', 'superseded', 6),
    ]);
    // Newest first inside each half, with the other half untouched.
    expect(ids(active())).toEqual(['newer-exec', 'older-open']);
    expect(active().textContent).toContain(t('steward.sectionActive'));
    expect(ids(past())).toEqual([]);
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(toggle().textContent).toContain('Show decided (5)');

    await fireEvent.click(toggle());
    await waitFor(() => expect(toggle().getAttribute('aria-expanded')).toBe('true'));
    expect(toggle().textContent).toContain('Hide decided (5)');
    expect(ids(past())).toEqual(['superseded-row', 'withdrawn-row', 'declined-row', 'failed-row', 'done-old']);
    expect(ids(active())).toEqual(['newer-exec', 'older-open']);

    await fireEvent.click(toggle());
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(ids(past())).toEqual([]);
  });

  it('says so when nothing is pending, and keeps the old empty message when there are no proposals at all', async () => {
    const folded = await mount([proposal('past-1', 'done', 1), proposal('past-2', 'failed', 2)]);
    expect(folded.view.getByText('Nothing waiting for your decision.')).toBeTruthy();
    expect(ids(folded.active())).toEqual([]);
    expect(ids(folded.past())).toEqual([]);
    expect(folded.toggle().getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(folded.toggle());
    await waitFor(() => expect(ids(folded.past())).toEqual(['past-2', 'past-1']));

    cleanup();
    api.listStewardProposals.mockResolvedValue([]);
    const empty = render(StewardProposals, props());
    await waitFor(() => expect(empty.getByText('No proposals yet.')).toBeTruthy());
    expect(empty.container.querySelector('[data-proposal-section]')).toBeNull();
  });
});

describe('one card owns three panes', () => {
  it('keeps the approval row out of both scrollers and bounds each scroll box', async () => {
    const {view, active} = await mount([proposal('p1', 'open', 1)]);
    const card = active().querySelector('[data-proposal="p1"]') as HTMLElement;
    const title = card.querySelector('[data-card-pane="title"]') as HTMLElement;
    const content = card.querySelector('[data-card-pane="content"]') as HTMLElement;
    const actions = card.querySelector('[data-card-pane="actions"]') as HTMLElement;
    expect(title).toBeTruthy(); expect(content).toBeTruthy(); expect(actions).toBeTruthy();
    expect(new Set([title, content, actions]).size).toBe(3);
    expect(content.contains(title)).toBe(false);
    expect(content.contains(actions)).toBe(false);
    expect(actions.contains(content)).toBe(false);

    const approve = view.getByRole('button', {name: 'Approve'});
    expect(actions.contains(approve)).toBe(true);
    expect(content.contains(approve)).toBe(false);
    // The card body (dl, sub-cards, impact, executing/result) really is in the
    // content pane, not left behind in the old flat article.
    expect(content.querySelector('dl')).toBeTruthy();
    expect(content.textContent).toContain('a reason');

    // Bounded flex column: the card cannot grow with its content.
    expect(css(card, 'display')).toBe('flex');
    expect(css(card, 'flex-direction')).toBe('column');
    expect(css(card, 'max-height')).toMatch(/px|vh/);
    // Title scrolls on its own; it never pushes the content pane away.
    expect(css(title, 'max-height')).toMatch(/px|vh/);
    expect(overflowY(title)).toBe('auto');
    // The content pane is the long-text scroller. This is the computed value of
    // the scoped rule Svelte injected; removing `max-height:52vh` from .body
    // turns it into '' and fails here.
    expect(css(content, 'max-height')).toMatch(/px|vh/);
    expect(overflowY(content)).toBe('auto');
    // The decision row is pinned: non-shrinking, separated, opaque.
    expect(css(actions, 'flex')).toBe('0 0 auto');
    expect(css(actions, 'border-top-width')).not.toBe('0px');
    expect(css(actions, 'background-color')).not.toBe('');
  });

  it('puts the executing and result branches inside the scrolling body', async () => {
    const running = await mount([proposal('p-run', 'executing', 1)]);
    const runningCard = running.active().querySelector('[data-proposal="p-run"]') as HTMLElement;
    expect(runningCard.querySelector('[data-card-pane="content"] .executing')).toBeTruthy();
    expect(runningCard.querySelector('[data-card-pane="actions"]')).toBeNull();
    cleanup();

    const decided = await mount([proposal('p-done', 'done', 1)]);
    await fireEvent.click(decided.toggle());
    const doneCard = decided.past().querySelector('[data-proposal="p-done"]') as HTMLElement;
    expect(doneCard.querySelector('[data-card-pane="content"] .result')).toBeTruthy();
  });
});

describe('the new section labels are translated, not copied', () => {
  it('has all five keys in all 8 languages, with the count in both past-toggle labels', () => {
    const keys = ['sectionActive', 'sectionPast', 'pastShow', 'pastHide', 'activeEmpty'].map(key => 'steward.' + key);
    langStore.set('en');
    const english = Object.fromEntries(keys.map(key => [key, t(key)]));
    for (const key of keys) expect(english[key], key).not.toBe(key);
    const copied: string[] = [];
    for (const lang of ['zh-CN', 'zh-TW', 'ja', 'ko', 'fr', 'de', 'es']) {
      langStore.set(lang);
      for (const key of keys) if (t(key) === english[key]) copied.push(`${lang} ${key}`);
      expect(t('steward.pastShow'), lang).toContain('{n}');
      expect(t('steward.pastHide'), lang).toContain('{n}');
    }
    langStore.set('en');
    expect(copied).toEqual([]);
    langStore.set('zh-CN');
    expect(t('steward.sectionActive')).toBe('待你决定');
    expect(t('steward.sectionPast')).toBe('已处理');
    expect(t('steward.pastShow')).toBe('展开已处理 ({n})');
    langStore.set('zh-TW');
    expect(t('steward.sectionActive')).toBe('待你決定');
    expect(t('steward.sectionPast')).toBe('已處理');
    langStore.set('en');
  });
});
