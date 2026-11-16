/** The generic plugin names no backend (design §4 rule; review criterion
 *  "boundary-independent"). What this gate covers, exactly - and what it does not:
 *
 *  1. The backend's NAME: every byte of every file under dsh-director (code,
 *     comments, tests, docs, python) except node_modules, __pycache__ and the
 *     recorded request bodies under test/fixtures (what the backend's driver sent),
 *     any case. Not covered: a name assembled at run time from pieces, as this file
 *     assembles it so that it does not contain it itself.
 *  2. The backend's VOCABULARY in string literals of .mjs/.js/.ts files: an /api/
 *     route, a state_<x> tool name, or the word State. Covered literal shapes:
 *     '...' and "..." on one line, and `...` template literals including ones that
 *     span lines. Not covered: text built by concatenation or at run time.
 *  3. IMPORTS of the adapter directory (dsh-driver): `import ... from '<spec>'`,
 *     `export ... from '<spec>'`, `import '<spec>'`, `import('<spec>')` and
 *     `require('<spec>')` with a literal specifier. Not covered: a computed
 *     specifier.
 *  The second test runs the same scanner over one sample of every covered shape,
 *  so the claim above is measured, not only stated. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const skip = new Set(['node_modules', 'fixtures', '__pycache__']);
const name = ['ait', 'elier'].join('');
const banned = new RegExp(name, 'i');
// (?<![a-z]) rather than \b: an MCP name such as mcp__x__state_graph_write puts an underscore,
// a word character, right before the tool name, where \b never matches.
const vocabulary = /\/api\/|(?<![a-z])state_[a-z]|\bState\b/;
const adapterImport = /(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(['"`])[^'"`]*dsh-driver[^'"`]*\1/;

async function files(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (skip.has(entry.name)) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await files(path));
    else if (entry.isFile()) out.push(path);
  }
  return out;
}

/** Every violation in one file's text, as "<line>: <reason>". */
export function scan(text, path) {
  const found = [];
  text.split('\n').forEach((line, i) => { if (banned.test(line)) found.push(`${i + 1}: backend name`); });
  if (!/\.(mjs|js|ts)$/.test(path)) return found;
  const lineOf = index => text.slice(0, index).split('\n').length;
  for (const match of text.matchAll(/`(?:\\[\s\S]|[^`\\])*`/g))
    if (vocabulary.test(match[0])) found.push(`${lineOf(match.index)}: backend vocabulary in a template literal`);
  text.split('\n').forEach((line, i) => {
    for (const [literal] of line.matchAll(/'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/g))
      if (vocabulary.test(literal)) { found.push(`${i + 1}: backend vocabulary in a string`); break; }
    if (adapterImport.test(line)) found.push(`${i + 1}: adapter import`);
  });
  return found;
}

test('no shipped file of dsh-director names the backend, carries its vocabulary in a string, or imports the adapter', async () => {
  const found = [];
  for (const file of await files(root))
    for (const hit of scan(await readFile(file, 'utf8'), file)) found.push(`${relative(root, file)}:${hit}`);
  assert.deepEqual(found, []);
});

test('the gate refuses one sample of every shape its header says it covers', () => {
  // The samples are assembled from pieces, like the name, so this file passes its own gate.
  const api = '/' + 'api/', st = 'Sta' + 'te', tool = 'sta' + 'te_graph_write', bt = String.fromCharCode(96), adapter = '../dsh-' + 'driver/runtime.mjs';
  const cases = [
    ['notes.md', 'a doc line that says ' + name.toUpperCase()],
    ['x.py', '# comment naming ' + name],
    ['x.mjs', '// comment naming ' + name],
    ['x.mjs', "const route = '" + api + "x/query/y';"],
    ['x.mjs', 'const tool = "mcp__x__' + tool + '";'],
    ['x.mjs', "const reason = 'you hold live " + st + " claims';"],
    ['x.mjs', 'const t = ' + bt + 'line one\n and the ' + st + ' on line two' + bt + ';'],
    ['x.mjs', "import { a } from '" + adapter + "';"],
    ['x.mjs', "export { a } from '" + adapter + "';"],
    ['x.mjs', "import '" + adapter + "';"],
    ['x.mjs', "const m = await import('" + adapter + "');"],
    ['x.mjs', 'const m = await import(' + bt + adapter + bt + ');'],
    ['x.mjs', "const m = require('" + adapter + "');"],
  ];
  for (const [path, text] of cases) assert.notDeepEqual(scan(text, path), [], 'not refused: ' + path + ': ' + text);
  // And it does not cry wolf on the plugin's ordinary wording.
  for (const text of ["const reason = 'you hold work that is not finished';", "import { x } from './lib/core.mjs';", '// a work item'])
    assert.deepEqual(scan(text, 'x.mjs'), [], text);
});
