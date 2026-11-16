/** The generic plugin carries nothing of its backend (design §4 rule; review
 *  criterion "boundary-independent"). What this gate covers, exactly:
 *
 *  WALK: every regular file under dsh-director, at any depth, any extension,
 *  except exactly two paths - `node_modules` at the plugin root (installed, not
 *  shipped) and `test/fixtures/request-bodies` (the recorded request bodies, which
 *  are what the backend's own driver sent) - and compiled `.pyc` files. Symbolic
 *  links are not followed, and the walk refuses to find one (the plugin ships real
 *  files only; its default templates are real files under todo/todo_templates). The walk must visit at
 *  least MIN_FILES files and every file in MUST_VISIT, so an empty or pruned walk
 *  is red, not green.
 *  NAME and VOCABULARY: every byte of every walked file - code, comments,
 *  docstrings, strings of any quoting, docs, data - is searched for the backend's
 *  name (any case), its capitalised entity word, its route prefix and its tool-name
 *  shapes (see `vocabulary`). There is no tokenizer, so no quoting, comment or
 *  extension can hide a match.
 *  IMPORTS: in .mjs/.js/.ts files, an import-from, export-from, side-effect import,
 *  import() or require() whose literal specifier names the adapter directory.
 *  NOT covered: text or specifiers assembled at run time from pieces (this file
 *  assembles its own tokens that way), and names the backend uses that are not in
 *  this list - the adapter side checks the plugin against the tool names it really
 *  registers (integrations/dsh-driver/plugin-boundary.test.mjs).
 *  The last test feeds the scanner one sample of every covered shape. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vocabulary, name, entity, route, toolShape } from '../tools/vocabulary.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
// The tokens come from tools/vocabulary.mjs, assembled there from pieces.
const word = (...parts) => parts.join('');
export { vocabulary };
const adapterImport = new RegExp('(?:\\bfrom\\s*|\\bimport\\s*\\(?\\s*|\\brequire\\s*\\(\\s*)([\'"`])[^\'"`]*' + word('dsh-', 'driver') + '[^\'"`]*\\1');
export const EXCLUDED = [`node_modules`, join('test', 'fixtures', 'request-bodies')];
export const MIN_FILES = 30;
export const MUST_VISIT = ['index.mjs', 'host.d.ts', 'roles.json', 'director.md', 'worker.md', join('lib', 'core.mjs'),
  join('lib', 'host.mjs'), join('hosts', 'local.mjs'), join('todo', 'dsh_todo', 'cli.py'), join('todo', 'dsh_todo', 'batch.py'),
  join('standalone', 'scenario.mjs'), join('standalone', 'run-in-container.sh'), join('test', 'boundary.test.mjs'),
  join('tools', 'vocabulary.mjs'), join('todo', 'todo_templates', 'change_files.yaml')];

/** Whether a plugin-relative path is excluded from the walk (exact paths only). */
export const excluded = rel => EXCLUDED.includes(rel) || rel.endsWith('.pyc');

export function walk(dir = root, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name), rel = relative(root, path);
    if (excluded(rel)) continue;
    if (entry.isSymbolicLink()) throw new Error(`a symbolic link in the plugin: ${rel} (ship real files)`);
    if (entry.isDirectory()) walk(path, out);
    else if (entry.isFile()) out.push(path);
  }
  return out;
}

/** Every violation in one file's text, as "<line>: <reason>". */
export function scan(text, path) {
  const found = [];
  text.split('\n').forEach((line, i) => {
    for (const [pattern, reason] of vocabulary) if (pattern.test(line)) found.push(`${i + 1}: ${reason}`);
    if (/\.(mjs|js|ts)$/.test(path) && adapterImport.test(line)) found.push(`${i + 1}: adapter import`);
  });
  return found;
}

test('the walk visits the plugin, not an empty or pruned tree', () => {
  const visited = walk().map(path => relative(root, path));
  assert.ok(visited.length >= MIN_FILES, `visited only ${visited.length} files: ${visited.join(', ')}`);
  for (const file of MUST_VISIT) assert.ok(visited.includes(file), `not visited: ${file}`);
  assert.ok(!visited.some(file => file.startsWith(EXCLUDED[1] + sep)), 'the recorded request bodies were walked');
});

test('no byte of any shipped plugin file carries the backend, and no code imports the adapter', () => {
  const found = [];
  for (const file of walk()) for (const hit of scan(readFileSync(file, 'utf8'), file)) found.push(`${relative(root, file)}:${hit}`);
  assert.deepEqual(found, []);
});

test('the gate refuses one sample of every shape its header says it covers', () => {
  const bt = String.fromCharCode(96), q3 = '"'.repeat(3), adapter = '../' + word('dsh-', 'driver') + '/runtime.mjs';
  const tool = 'mcp__x__' + toolShape + 'graph_write';
  const cases = [
    ['notes.md', 'a doc line that says ' + name.toUpperCase()],
    ['x.py', '# comment naming ' + name],
    ['x.py', q3 + 'One ' + entity + ' record per item.' + q3],
    ['x.json', '{"note": "the ' + entity + ' attempt"}'],
    ['x.sh', '# calls ' + route + 'x'],
    ['x.mjs', '// comment naming ' + entity],
    ['x.mjs', "if (/don't/.test(s)) throw new Error('" + tool + " is refused');"],
    ['x.mjs', "const route = '" + route + "x/query/y';"],
    ['x.mjs', 'const t = ' + bt + 'line one\n and the ' + entity + ' on line two' + bt + ';'],
    ['x.mjs', "import { a } from '" + adapter + "';"],
    ['x.mjs', "export { a } from '" + adapter + "';"],
    ['x.mjs', "import '" + adapter + "';"],
    ['x.mjs', "const m = await import('" + adapter + "');"],
    ['x.mjs', 'const m = await import(' + bt + adapter + bt + ');'],
    ['x.mjs', "const m = require('" + adapter + "');"],
  ];
  for (const [path, text] of cases) assert.notDeepEqual(scan(text, path), [], 'not refused: ' + path + ': ' + text);
  for (const text of ["const reason = 'you hold work that is not finished';", "import { x } from './lib/core.mjs';",
    'state_dir = setting("STATE_DIR")', '// a work state, lower case'])
    assert.deepEqual(scan(text, 'x.mjs'), [], 'false alarm: ' + text);
  // The walk excludes exact paths only: a fixtures directory anywhere else is walked.
  assert.equal(excluded(join('lib', 'fixtures', 'notes.mjs')), false);
  assert.equal(excluded(join('lib', 'node_modules', 'x.mjs')), false);
  assert.equal(excluded(EXCLUDED[1]), true);
});
