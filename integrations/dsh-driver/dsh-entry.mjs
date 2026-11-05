/** One owner for "which DSH binary may this test suite measure?".
 *
 * The repository pins `@deepseek-ai/dsh` in package.json; a test run is only
 * evidence about the pin when it measured exactly that version. This module
 * therefore never guesses: every candidate binary is attributed to the package
 * manifest that owns it, and only a candidate whose version equals the pin is
 * ever returned as `measured`. A suite that finds nothing is `unmeasured` (an
 * environment fact — it must not go red); a suite that finds only a different
 * version is `refused` (an actionable mistake — it must not go green either).
 *
 * Every test file in this directory that boots the real harness needs the same
 * answer, so `measuredEntryForTest` is the single call site shape they share:
 *
 *   const entry = await measuredEntryForTest(t);
 *   if (!entry) return;                       // the test reported itself unmeasured
 *
 * `withoutCallerMode` exists because the *caller's shell* must not decide which
 * runtime a driver boots into: `AITELIER_DSH_EXECUTION=host` in the invoking
 * environment silently changes the fixture's branch and makes these tests
 * measure the wrong thing.
 */
import { readFile, realpath, stat, readdir } from 'node:fs/promises';
import { dirname, join, basename, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

/** The package whose version the tests are pinned to. */
export const DSH_PACKAGE = '@deepseek-ai/dsh';

/** Directory holding this module — every relative default hangs off it, never off cwd. */
export const packageRoot = dirname(fileURLToPath(import.meta.url));

/** Variables that select the runtime a driver boots into. The test owns them. */
export const executionModeKeys = ['AITELIER_DSH_EXECUTION', 'DSH_PERMISSION_MODE'];

const MAX_WALK = 8;

/** The committed pin, read from the same manifest npm installs from. */
export async function pinnedDshVersion(root = packageRoot) {
  const file = join(root, 'package.json');
  const manifest = JSON.parse(await readFile(file, 'utf8'));
  const version = manifest.dependencies?.[DSH_PACKAGE];
  if (typeof version !== 'string' || !version)
    throw new Error(`${file} does not pin ${DSH_PACKAGE}`);
  return version;
}

/** Copy of `env` without an inherited execution mode, so a test can set its own. */
export function withoutCallerMode(env = process.env) {
  const copy = { ...env };
  for (const key of executionModeKeys) delete copy[key];
  return copy;
}

/** Candidate binaries, in the only order that matters: most explicit first. */
export function dshEntryCandidates({ env = process.env, home = homedir(), root = packageRoot, systemRoots = true } = {}) {
  const candidates = [];
  const add = (path, source) => {
    if (typeof path === 'string' && path) candidates.push({ path: resolve(path), source });
  };
  add(env.AITELIER_DSH_TEST_ENTRY, 'AITELIER_DSH_TEST_ENTRY');
  add(env.AITELIER_DSH_BIN, 'AITELIER_DSH_BIN');
  add(join(root, 'node_modules', '.bin', 'dsh'), 'this checkout');
  if (systemRoots) add(join(installRoot({ env, home }), 'node_modules', '.bin', 'dsh'), 'host install');
  if (systemRoots) add(join('/', 'opt', 'aitelier-dsh', 'node_modules', '.bin', 'dsh'), 'container install');
  return candidates;
}

/** Where deploy/install_dsh_host.py installs the runtime when it is not in the checkout. */
export function installRoot({ env = process.env, home = homedir() } = {}) {
  return resolve(env.AITELIER_DSH_HOME ?? join(home, '.local/share', 'aitelier-dsh'));
}

/** Released fingerprints beside `installRoot`, newest first. */
export async function releaseEntries({ env = process.env, home = homedir() } = {}) {
  const root = installRoot({ env, home });
  let names;
  try { names = await readdir(join(root, 'releases')); }
  catch (error) { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return []; throw error; }
  return names.slice().sort().reverse().map(name =>
    ({ path: join(root, 'releases', name, 'node_modules', '.bin', 'dsh'), source: `host release ${name}` }));
}

async function readJson(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return null; throw error; }
}

/** The @deepseek-ai/dsh manifest that owns this binary, or null when unattributable. */
async function owningManifest(binary) {
  const starts = [];
  try { starts.push(dirname(await realpath(binary))); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  starts.push(dirname(resolve(binary)));
  for (const start of starts) {
    let directory = start;
    for (let depth = 0; depth < MAX_WALK; depth++) {
      const manifest = await readJson(join(directory, 'package.json'));
      if (manifest?.name === DSH_PACKAGE) return manifest;
      // `node_modules/.bin/dsh` need not be a symlink on every platform.
      if (basename(directory) === 'node_modules') {
        const scoped = await readJson(join(directory, DSH_PACKAGE, 'package.json'));
        if (scoped?.name === DSH_PACKAGE) return scoped;
      }
      const parent = dirname(directory);
      if (parent === directory) break;
      directory = parent;
    }
  }
  return null;
}

/**
 * Decide which binary may be measured.
 * @returns {Promise<{status: 'measured'|'unmeasured'|'refused', pinned: string,
 *   version: string|null, entry: string|null, source: string|null, reason: string|null,
 *   searched: string[], mismatches: {path: string, source: string, version: string|null}[]}>}
 */
export async function resolveDshEntry({ env = process.env, home = homedir(), root = packageRoot, systemRoots = true } = {}) {
  const pinned = await pinnedDshVersion(root);
  const searched = [], mismatches = [];
  const candidates = dshEntryCandidates({ env, home, root, systemRoots });
  if (systemRoots) candidates.splice(candidates.length - 1, 0, ...await releaseEntries({ env, home }));
  for (const { path, source } of candidates) {
    searched.push(path);
    let info;
    try { info = await stat(path); }
    catch (error) { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') continue; throw error; }
    if (!info.isFile()) continue;
    const manifest = await owningManifest(path);
    if (manifest?.version !== pinned) {
      mismatches.push({ path, source, version: manifest?.version ?? null });
      continue;
    }
    return { status: 'measured', pinned, version: manifest.version, entry: await realpath(path), source,
      reason: null, searched, mismatches };
  }
  if (mismatches.length) {
    const detail = mismatches.map(m => `${m.path} (${m.source}) is ${m.version ?? 'of unknown version'}`).join('; ');
    return { status: 'refused', pinned, version: null, entry: null, source: null,
      reason: `refusing to measure ${DSH_PACKAGE} ${pinned}: ${detail}`, searched, mismatches };
  }
  return { status: 'unmeasured', pinned, version: null, entry: null, source: null,
    reason: `no installed ${DSH_PACKAGE} ${pinned} to measure; looked in: ${searched.join(', ')}`,
    searched, mismatches };
}

/**
 * The single call shape every pinned-DSH test shares: returns the measured
 * binary, marks the test unmeasured (and returns null) when none is installed,
 * and fails loudly when only a differently-versioned binary exists.
 */
export async function measuredEntryForTest(t, options = {}) {
  const resolution = await resolveDshEntry(options);
  if (resolution.status === 'refused') throw new Error(resolution.reason);
  if (resolution.status === 'unmeasured') {
    t.skip(resolution.reason);
    return null;
  }
  t.diagnostic(`measuring ${DSH_PACKAGE} ${resolution.version} (${resolution.source}): ${resolution.entry}`);
  return resolution.entry;
}
