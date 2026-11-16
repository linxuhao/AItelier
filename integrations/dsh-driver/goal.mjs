import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { dirname, basename, join } from 'node:path';
import { appendFile } from 'node:fs/promises';

const git = promisify(execFile);

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
