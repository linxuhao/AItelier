import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { dirname, basename, join } from 'node:path';
import { appendFile, lstat, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';

const git = promisify(execFile);

/** The deployment's own director mandate: `director-goal.md` in the AItelier data
 *  root ($AITELIER_HOME, else ~/.AItelier - the rule of core/datadir.py), outside the
 *  repository. It is an authorization the operator grants; the repository's goal.md
 *  is only the neutral default. */
export const GOAL_OVERRIDE_NAME = 'director-goal.md';

export function goalOverrideFile(env = process.env) {
  return join(env.AITELIER_HOME || join(homedir(), '.AItelier'), GOAL_OVERRIDE_NAME);
}

/** The mandate template this process runs with: the deployment's override when it
 *  exists, else `defaultFile` (the repository's goal.md).
 *
 *  Returns { file, override, text } or, for an override that exists but cannot be
 *  used (unreadable, a dangling link, empty), { file, override: true, error }: never
 *  the default in its place, because the default would then be re-seeded into every
 *  active director as if the owner had changed the mandate. Only a missing default
 *  (a broken checkout) throws, as it always did. */
export async function loadGoalTemplate(defaultFile, env = process.env) {
  const file = goalOverrideFile(env);
  const refuse = why => ({ file, override: true,
    error: new Error(`director goal override ${file} ${why}; refusing to fall back to the repository default`) });
  let text;
  try {
    text = await readFile(file, 'utf8');
  } catch (error) {
    if (error?.code !== 'ENOENT') return refuse(`cannot be read (${error?.code ?? error})`);
    // ENOENT is also what a link to a missing file answers: that override is present.
    try { await lstat(file); return refuse('is a link to a missing file'); } catch { /* absent */ }
    return { file: defaultFile, override: false, text: await readFile(defaultFile, 'utf8') };
  }
  if (!text.trim()) return refuse('is empty');
  return { file, override: true, text };
}

/** Git commit of the goal.md the runtime loaded, or 'unknown' (never throws).
 *  Tries the repository holding the loaded file, then AITELIER_TODO_CWD. */
export async function goalMdCommit(goalFile, { cwd, env = process.env } = {}) {
  const attempts = [[dirname(goalFile), basename(goalFile)]];
  if (cwd) attempts.push([cwd, ':/integrations/dsh-driver/goal.md']);
  for (const [dir, file] of attempts) {
    try {
      const { stdout } = await git('git', ['-C', dir, 'log', '-1', '--format=%H', '--', file], { env, timeout: 5000 });
      if (/^[0-9a-f]{40,64}$/.test(stdout.trim())) return stdout.trim();
    } catch { /* git missing, not a repository, timeout: try the next, else unknown */ }
  }
  return 'unknown';
}

// The goal binding itself is dsh-director's; this deployment re-exports it.
export { ROUND_TOP_UP_BELOW, ROUND_TOP_UP_TO, bindDriverGoal, driverGoalSection } from '../dsh-director/lib/goal.mjs';


/**
 * Append one re-seed record to the driver's goal-seed.jsonl trace.
 *
 * Returns { written: true } or { written: false, error } - it NEVER throws. The
 * trace witnesses a change that goals.edit has already applied, so a failed write
 * must not be able to reject boot(); boot()'s only call site turns a rejection into
 * appExit(1) and systemd then restart-loops every resident driver on the host
 * because one directory was momentarily unwritable.
 */
export async function appendGoalSeedRecord(stateDir, record) {
  try {
    await appendFile(join(stateDir, 'goal-seed.jsonl'), JSON.stringify(record) + '\n', { mode: 0o600 });
    return { written: true };
  } catch (error) {
    return { written: false, error: String(error?.code ?? error).slice(0, 120) };
  }
}
