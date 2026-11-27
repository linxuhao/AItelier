/** Open issues at dormancy (owner 2026-11-27: soft, not a refusal). An idle_dormant that
 *  succeeds also says what is open: how many issues, by kind, and up to TRIAGE_SHOWN of the
 *  "untracked" ones - open, with no work item and no work left that would bring them back (the
 *  host decides: for example, no linked work still to be done) - for the director to triage
 *  before it sleeps again. Open issues never refuse dormancy: a project can carry hundreds of
 *  them, and a refusal would keep the director awake on its whole backlog (or push it to
 *  close issues just to sleep).
 *
 *  Which untracked issues are named rotates: never named first, then the least recently
 *  named, oldest first within each. An issue the director leaves (it cannot decide it now)
 *  gives way to the next ones at the next sleep instead of being named every time; the
 *  record (triage-shown.json in the state directory) keeps only issues still untracked. */
import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

export const TRIAGE_SHOWN = 5;

/** The `n` untracked issues to name now, given when each was last named (`shown`: id -> ms). */
export function pickTriage(untracked, shown = {}, n = TRIAGE_SHOWN) {
  return [...untracked].sort((a, b) => (shown[a.id] ?? 0) - (shown[b.id] ?? 0)
    || String(a.created_at ?? '').localeCompare(String(b.created_at ?? ''))).slice(0, n);
}

export class TriageRotation {
  constructor(stateDir) { this.dir = stateDir; this.file = join(stateDir, 'triage-shown.json'); }

  /** Picks the issues to name now and records them as named at `now`. */
  async next(untracked, now = Date.now(), n = TRIAGE_SHOWN) {
    let shown = {};
    try { shown = JSON.parse(await readFile(this.file, 'utf8')); } catch { shown = {}; }
    if (!shown || typeof shown !== 'object' || Array.isArray(shown)) shown = {};
    const picked = pickTriage(untracked, shown, n);
    const open = new Set(untracked.map(issue => issue.id));
    const kept = Object.fromEntries(Object.entries(shown).filter(([id]) => open.has(id)));
    for (const issue of picked) kept[issue.id] = now;
    await mkdir(this.dir, { recursive: true });
    const tmp = this.file + '.tmp';
    await writeFile(tmp, JSON.stringify(kept));
    await rename(tmp, this.file);
    return picked;
  }
}

/** The idle_dormant result's `open_issues`, from the host's answer (host.d.ts OpenIssues) and
 *  the issues picked to name; `note` (texts.triageNote) only when there is something to triage. */
export function openIssuesSummary(answer, picked, note, now = Date.now()) {
  const untracked = Array.isArray(answer.untracked) ? answer.untracked.length : 0;
  return {
    total: answer.total, by_kind: answer.by_kind, untracked,
    ...(picked.length ? {
      triage: picked.map(issue => ({ id: issue.id, kind: issue.kind, title: String(issue.title ?? '').slice(0, 200),
        ...(issue.created_at ? { age_days: Math.max(0, Math.floor((now - Date.parse(issue.created_at)) / 86400000)) } : {}) })),
      note,
    } : {}),
  };
}
