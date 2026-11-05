import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

// This file guards the suite's own composition, not its behaviour.
//
// History, because a guard whose reasons are forgotten gets deleted: on
// 2026-11-05 `npm test` listed ten files while the directory held thirteen, and
// the unlisted one was dsh-entry.test.mjs - the guards that a refusal must never
// become a skip. An hour later a NEW test file carried the hand-rolled install
// expression the project had just finished removing, and main went red on a clean
// checkout the moment it was merged. Both rules had been written down once and
// had decayed.
//
// The first version of this guard was itself reviewed adversarially, and three of
// its choices did not survive:
//   - it matched the literal string './node_modules/.bin/dsh', so `new URL('...')`,
//     `join(process.cwd(), 'node_modules', '.bin', 'dsh')`, concatenation and
//     `join('.bin', 'dsh')` all got through - five of six real spellings - while a
//     comment mentioning the path was flagged. It reddened the file that documents
//     the rule and greened the file that breaks it.
//   - it read one directory with an exact suffix, so a test file in a subdirectory,
//     a .test.js, or a differently-cased name was green while never running.
//   - and it could not see its own removal from the script, the one omission its own
//     message promises to catch. That observer now lives outside this file, in
//     tests/unit/test_driver_suite_lists_its_files.py, which pytest collects. The
//     chain ends there: if that file is deleted too, nothing here notices - which is
//     why it is deliberately tiny.

const here = dirname(fileURLToPath(import.meta.url));
const ALLOWED = new Set(['dsh-entry.mjs', 'dsh-entry.test.mjs', 'suite-composition.test.mjs']);
const TEST_FILE = /\.(test|spec)\.(mjs|cjs|js)$/i;
const WINDOW = 400;

// Comments are prose; code is what resolves. String literals are NOT stripped:
// `join('node_modules', '.bin', 'dsh')` states its intent in strings, and that is
// exactly the spelling the old literal match could not see.
const stripComments = source => source
  .replace(/\/\*[\s\S]*?\*\//g, ' ')
  .replace(/(^|[^:\\])\/\/[^\n]*/g, '$1');

// True when the text would RESOLVE an executable under node_modules/.bin: some
// occurrence of the node_modules segment, a .bin segment and the name, inside one
// expression-sized window, in any of the ways people write it.
const resolvesTheEntry = text => {
  const flat = stripComments(text).replace(/\s+/g, ' ');
  for (let i = flat.indexOf('node_modules'); i !== -1; i = flat.indexOf('node_modules', i + 1)) {
    const window = flat.slice(Math.max(0, i - 200), i + WINDOW);
    if (window.includes('.bin') && /(^|[^\w-])dsh([^\w-]|$)/.test(window)) return true;
  }
  return false;
};

const walk = async (dir, out = []) => {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') await walk(full, out);
    } else if (entry.isFile() && TEST_FILE.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
};

const scriptOf = async () => {
  const pkg = JSON.parse(await readFile(join(here, 'package.json'), 'utf8'));
  return String(pkg.scripts?.test ?? '');
};

const listedFiles = async () => {
  const script = await scriptOf();
  return new Set(script.split(/\s+/)
    .filter(token => TEST_FILE.test(token))
    .map(token => relative(here, join(here, token))));
};

test('every test file the runner would find is run by npm test', async () => {
  const script = await scriptOf();
  // A glob or bare discovery runs whatever exists; comparing names would then be
  // wrong rather than strict, so the honest check is that something runs them.
  const discovers = /--test(\s|$)/.test(script) && (/[*?]/.test(script) || !TEST_FILE.test(script));
  const present = (await walk(here)).map(file => relative(here, file)).sort();
  assert.ok(present.length > 0, 'found no test files at all - the discovery is broken, not the suite');
  if (discovers) return;
  const listed = await listedFiles();
  const missing = present.filter(file => !listed.has(file));
  assert.deepEqual(missing, [], `npm test does not run: ${missing.join(', ')}. Add them to package.json's "test" script - a file nobody runs is a guard nobody has.`);
  const absent = [...listed].filter(file => !present.includes(file)).sort();
  assert.deepEqual(absent, [], `npm test names files that do not exist: ${absent.join(', ')}`);
});

test('the script cannot swallow a failure', async () => {
  const script = await scriptOf();
  assert.ok(!/\|\|\s*(true|:)\b/.test(script), `the test script contains "|| true": ${script}. A suite that cannot fail is a suite that cannot report.`);
});

test('only the resolver and its unit test resolve the DSH entry', async () => {
  const files = await readdir(here);
  const offenders = [];
  for (const file of files) {
    if (ALLOWED.has(file) || !file.endsWith('.mjs')) continue;
    if (resolvesTheEntry(await readFile(join(here, file), 'utf8'))) offenders.push(file);
  }
  assert.deepEqual(offenders, [], `${offenders.join(', ')} resolve the DSH entry themselves. Call measuredEntryForTest(t) from dsh-entry.mjs instead - a test that picks its own install measures whatever shell invoked it.`);
});
