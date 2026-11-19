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

/** The one definition of what a git call IS: the identity, safe.directory and quotePath
 *  arguments, the `umask 077` sh wrapper every call goes through, the working directory and
 *  the allowlisted environment. `git()` spawns exactly this spec, and the resident grep helper
 *  (grep-helper.mjs) is handed the same spec to run, so a grep the helper runs is the command
 *  this process would have run - forked from a small process instead of a big one. Nothing
 *  else may build a git command line.
 *
 *  It travels to the helper as JSON, so it holds strings only: a Buffer, a function or a
 *  getter here would silently reach the other side as something else. */
export function gitSpawnSpec(dir, args, home = dir) {
  return {
    command: '/bin/sh',
    // umask 077 for every file git itself creates (COMMIT_EDITMSG, index, ...): the
    // repository's objects follow core.sharedRepository=0600, these follow the umask.
    argv: ['-c', 'umask 077 && exec git "$@"', 'git', '-c', 'user.name=dsh-director-memory',
      '-c', 'user.email=noreply@localhost', '-c', 'safe.directory=' + dir,
      '-c', 'core.quotePath=false', ...args],
    cwd: dir,
    env: gitEnv(home),
  };
}

/** Run git in `dir`. Resolves {code, stdout(Buffer), stderr, killed}; never rejects for a
 *  non-zero exit. `timeoutMs` kills the child (SIGKILL) and resolves with killed: true and
 *  whatever it printed so far; `pid` is reported so a test can prove the child is gone.
 *  `onStdout(chunk)` takes the output as it comes instead (stdout is then empty). */
export function git(dir, args, { input, timeoutMs = 60000, home = dir, onSpawn, onStdout } = {}) {
  return new Promise((resolve, reject) => {
    const spec = gitSpawnSpec(dir, args, home);
    const child = spawn(spec.command, spec.argv, { cwd: spec.cwd, env: spec.env, stdio: ['pipe', 'pipe', 'pipe'] });
    onSpawn?.(child.pid);
    const out = [], err = [];
    let killed = false;
    const timer = timeoutMs > 0 ? setTimeout(() => { killed = true; child.kill('SIGKILL'); }, timeoutMs) : null;
    child.stdout.on('data', d => (onStdout ? onStdout(d) : out.push(d)));
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
