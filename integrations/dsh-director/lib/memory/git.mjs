/** git for the memory repository (design 3.3), with an allowlisted environment.
 *
 * The host process may carry GIT_CONFIG_COUNT/KEY_i/VALUE_i and the like (this host
 * did, and a bare git reading them failed fatally), so nothing of the caller's
 * environment reaches git except PATH; identity and safe.directory are explicit. */
import { spawn } from 'node:child_process';

export function gitEnv(home) {
  return { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home, LANG: 'C', LC_ALL: 'C',
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0' };
}

/** Run git in `dir`. Resolves {code, stdout(Buffer), stderr, killed}; never rejects for a
 *  non-zero exit. `timeoutMs` kills the child (SIGKILL) and resolves with killed: true and
 *  whatever it printed so far; `pid` is reported so a test can prove the child is gone. */
export function git(dir, args, { input, timeoutMs = 60000, home = dir, onSpawn } = {}) {
  return new Promise((resolve, reject) => {
    const full = ['-c', 'user.name=dsh-director-memory', '-c', 'user.email=noreply@localhost',
      '-c', 'safe.directory=' + dir, '-c', 'core.quotePath=false', ...args];
    // umask 077 for every file git itself creates (COMMIT_EDITMSG, index, ...): the
    // repository's objects follow core.sharedRepository=0600, these follow the umask.
    const child = spawn('/bin/sh', ['-c', 'umask 077 && exec git "$@"', 'git', ...full],
      { cwd: dir, env: gitEnv(home), stdio: ['pipe', 'pipe', 'pipe'] });
    onSpawn?.(child.pid);
    const out = [], err = [];
    let killed = false;
    const timer = timeoutMs > 0 ? setTimeout(() => { killed = true; child.kill('SIGKILL'); }, timeoutMs) : null;
    child.stdout.on('data', d => out.push(d));
    child.stderr.on('data', d => err.push(d));
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => {
      clearTimeout(timer);
      resolve({ code, stdout: Buffer.concat(out), stderr: Buffer.concat(err).toString('utf8'), killed, pid: child.pid });
    });
    child.stdin.on('error', () => {});
    if (input !== undefined) child.stdin.end(input); else child.stdin.end();
  });
}

/** Like git(), but a non-zero exit (or a kill) rejects, naming the command and git's words. */
export async function gitOk(dir, args, options) {
  const r = await git(dir, args, options);
  if (r.code !== 0 || r.killed) {
    const error = new Error(`git ${args[0]} failed${r.killed ? ' (killed at timeout)' : ''}: ${r.stderr.trim().slice(0, 300)}`);
    error.git = r;
    throw error;
  }
  return r;
}
