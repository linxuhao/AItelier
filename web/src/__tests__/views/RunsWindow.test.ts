/**
 * The runs list is WINDOWED. GET /api/runs answers with only the rows inside
 * its own `window_days` plus {window_days, total_count, returned_count}, and
 * GET /api/runs?include_all=true answers with every row and the same keys.
 *
 * What this file proves, one test per criterion:
 *
 *  1. The page names the window and "N of M shown" from the RESPONSE — two
 *     different responses must produce two different sentences, so a hardcoded
 *     window or a hardcoded count cannot pass. The numbers are asserted even
 *     though the stubbed `runs` array is deliberately shorter than
 *     `returned_count`, so counting the rows on hand fails too.
 *  2. ONE control asks for everything: it issues the include_all=true URL and
 *     the older row the windowed answer did not contain appears; switching it
 *     off issues the plain URL again and that row is gone. The assertions are
 *     on the actual request URLs, not only on what is rendered.
 *  3. The timed poll never carries include_all — not before the control is
 *     touched, not while the archive is on screen, and the request after
 *     switching back is the plain one.
 *
 * `fetch` is stubbed rather than `lib/api`, because the criterion is about the
 * URL the page actually puts on the wire. Only setInterval/clearInterval are
 * faked, so the 10 s poll is driven explicitly while findByText/waitFor still
 * run on real time (the same approach as DriverConversationRefresh.test.ts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { connectionStore } from '../../stores/connection';
import { langStore } from '../../stores/i18n';

import UnifiedDashboard from '../../views/UnifiedDashboard.svelte';

const POLL_MS = 10000;

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });

/** The row every windowed answer carries. */
const LIVE = {
  project_id: 'gen-live',
  name: 'live generated run',
  repo_less: true,
  config_name: 'gen_cac40',
  status: 'completed',
  updated_at: 20,
};
/** The row only the include_all answer carries: old enough to be outside the
 *  window, and a repo-less run so this page renders it. */
const OLD = {
  project_id: 'gen-old',
  name: 'archived generated run',
  repo_less: true,
  config_name: 'gen_old',
  status: 'completed',
  updated_at: 1,
};

/** server/contract shape: windowed answer + the same rows for include_all. */
function responses(windowed: unknown, everything: unknown) {
  return { windowed, everything };
}

const DEFAULT = responses(
  { runs: [LIVE], window_days: 30, total_count: 1545, returned_count: 243 },
  { runs: [LIVE, OLD], window_days: 30, total_count: 1545, returned_count: 1545 },
);

/** Every URL the page sent to the runs endpoint, in order. */
let runsUrls: string[] = [];

/** Stub `fetch` and record the runs requests so a test can assert the WIRE. */
function installFetch(pair = DEFAULT) {
  runsUrls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.startsWith('/api/runs')) {
        runsUrls.push(url);
        return json(url.indexOf('include_all=true') !== -1 ? pair.everything : pair.windowed);
      }
      if (url.startsWith('/api/repos')) return json([]);
      if (url.startsWith('/api/pipelines')) return json({ pipelines: [] });
      return json({});
    }),
  );
}

const note = (view: { container: HTMLElement }) =>
  view.container.querySelector('.runs-window-note') as HTMLElement | null;
const windowBox = (view: { container: HTMLElement }) =>
  view.container.querySelector('[data-runs-window]') as HTMLElement | null;
const control = (view: { container: HTMLElement }) =>
  view.container.querySelector('[data-runs-include-all]') as HTMLButtonElement | null;

async function windowedNoteText(view: { container: HTMLElement }): Promise<string> {
  await waitFor(() => expect(note(view)).toBeTruthy());
  return (note(view) as HTMLElement).textContent ?? '';
}

beforeEach(() => {
  cleanup();
  vi.resetAllMocks();
  langStore.set('en');
  authStore.set({ canWrite: true, permissionResolved: true, email: 'owner@local' });
  connectionStore.set({ connectionOk: true, reconnectAttempt: 0 } as never);
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  installFetch();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('RunsWindow: the window is named from the response', () => {
  it('names the 30-day window and how many of the known rows are shown', async () => {
    const view = render(UnifiedDashboard);
    const text = await windowedNoteText(view);

    // 243 is `returned_count`, NOT the one row in the stubbed `runs` array.
    expect(text).toContain('30 days');
    expect(text).toContain('243 of 1545');
    // ... and the rows it is not showing: total_count - returned_count.
    expect(text).toContain('1302');
    expect(windowBox(view)?.getAttribute('data-runs-window')).toBe('30');
    expect(windowBox(view)?.getAttribute('data-runs-returned')).toBe('243');
    expect(windowBox(view)?.getAttribute('data-runs-total')).toBe('1545');
    expect(windowBox(view)?.getAttribute('data-runs-hidden')).toBe('1302');
    // The first fetch is the plain windowed one.
    expect(runsUrls[0]).toBe('/api/runs');
  });

  it('renders a different response differently, so no number here is hardcoded', async () => {
    installFetch(
      responses(
        { runs: [LIVE], window_days: 7, total_count: 9, returned_count: 2 },
        { runs: [LIVE, OLD], window_days: 7, total_count: 9, returned_count: 9 },
      ),
    );
    const view = render(UnifiedDashboard);
    const text = await windowedNoteText(view);

    expect(text).toContain('7 days');
    expect(text).toContain('2 of 9');
    expect(text).toContain('7'); // 9 - 2 rows hidden
    expect(text).not.toContain('30 days');
    expect(text).not.toContain('243');
    expect(text).not.toContain('1545');
    expect(windowBox(view)?.getAttribute('data-runs-window')).toBe('7');
    expect(windowBox(view)?.getAttribute('data-runs-hidden')).toBe('7');
  });
});

describe('RunsWindow: one control asks for everything', () => {
  it('fetches include_all=true and drops the older row again when switched off', async () => {
    const view = render(UnifiedDashboard);
    await view.findByText('live generated run');
    expect(view.queryByText('archived generated run')).toBeNull();
    // Exactly ONE control, and it is not rendered once per row or per section.
    expect(view.container.querySelectorAll('[data-runs-include-all]')).toHaveLength(1);
    expect(control(view)?.getAttribute('aria-pressed')).toBe('false');

    await fireEvent.click(control(view) as HTMLButtonElement);
    await view.findByText('archived generated run');
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs?include_all=true');
    expect(control(view)?.getAttribute('aria-pressed')).toBe('true');
    // The windowed row is still there: asking for all ADDS rows.
    expect(view.queryByText('live generated run')).toBeTruthy();

    await fireEvent.click(control(view) as HTMLButtonElement);
    await waitFor(() => expect(view.queryByText('archived generated run')).toBeNull());
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs');
    expect(control(view)?.getAttribute('aria-pressed')).toBe('false');
    // Switching back is a re-request, not a filter over the archive already held.
    expect(runsUrls.filter((u) => u.indexOf('include_all') !== -1)).toHaveLength(1);
  });
});

describe('RunsWindow: the timed poll stays windowed', () => {
  it('polls without include_all, before, during and after asking for everything', async () => {
    const view = render(UnifiedDashboard);
    await view.findByText('live generated run');
    const initial = runsUrls.length;
    expect(runsUrls[initial - 1]).toBe('/api/runs');

    // 1. The ordinary poll.
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await waitFor(() => expect(runsUrls.length).toBe(initial + 1));
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs');

    // 2. Ask for everything ...
    await fireEvent.click(control(view) as HTMLButtonElement);
    await view.findByText('archived generated run');
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs?include_all=true');

    // ... and the next timed request is STILL the windowed one, which must not
    // silently undo the view the reader asked for.
    const duringArchive = runsUrls.length;
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await waitFor(() => expect(runsUrls.length).toBe(duringArchive + 1));
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs');
    expect(view.queryByText('archived generated run')).toBeTruthy();

    // 3. Back to the window: the request is plain and the poll keeps polling plain.
    await fireEvent.click(control(view) as HTMLButtonElement);
    await waitFor(() => expect(view.queryByText('archived generated run')).toBeNull());
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs');

    const afterToggle = runsUrls.length;
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await waitFor(() => expect(runsUrls.length).toBe(afterToggle + 1));
    expect(runsUrls[runsUrls.length - 1]).toBe('/api/runs');

    // Over the whole test exactly one request ever asked for the archive.
    expect(runsUrls.filter((u) => u.indexOf('include_all') !== -1)).toEqual([
      '/api/runs?include_all=true',
    ]);
  });
});
