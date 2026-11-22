/** The dsh-director plugin as seen from this adapter, inside the repository's gate.
 *
 *  1. Runs the plugin's own test files in this suite (they import nothing of the
 *     adapter), so `node --test` here - which tests/integration/
 *     test_driver_node_suite_runs.py runs - fails when the plugin's suite does.
 *  2. Checks the plugin against the vocabulary this adapter ACTUALLY has, derived
 *     rather than listed: every tool name runtime.mjs registers, every tool name
 *     aitelier-roles.mjs adds (with and without its MCP prefix), that MCP prefix,
 *     and the source kinds and section prefix of aitelier-texts.mjs. None of them
 *     may appear in any byte of any file the plugin's boundary walk visits.
 *  3. Holds the plugin's own `npm test` script to this adapter's rule for script shape -
 *     one `node --test` invocation, no chain, no glob - with one difference the plugin's
 *     tree forces: the script must NAME its test files, because bare `node --test` in
 *     integrations/dsh-director also discovers test/fixtures/memory-crash.mjs, a fixture
 *     that is SIGKILLed, not a test. The declared files are read from
 *     integrations/dsh-director/package.json, and which of them run HERE is read from
 *     this file's own import statements. A test file the plugin's runner names but this
 *     suite never imports is a file the repository gate never runs - it fails the test
 *     below by name. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import '../dsh-director/test/host.test.mjs';
import '../dsh-director/test/local-host.test.mjs';
import '../dsh-director/test/outbox.test.mjs';
import '../dsh-director/test/capacity.test.mjs';
import '../dsh-director/test/resume.test.mjs';
import '../dsh-director/test/request-bodies.test.mjs';
import '../dsh-director/test/memory.test.mjs';
import '../dsh-director/test/memory-purge.test.mjs';
import '../dsh-director/test/memory-recovery.test.mjs';
import '../dsh-director/test/memory-remote.test.mjs';
import '../dsh-director/test/memory-search.test.mjs';
import '../dsh-director/test/memory-vector.test.mjs';
import '../dsh-director/test/memory-perf.test.mjs';
import '../dsh-director/test/memory-grep-helper.test.mjs';
import '../dsh-director/test/steward-tools.test.mjs';
import '../dsh-director/test/channels.test.mjs';
import '../dsh-director/test/clock.test.mjs';
import '../dsh-director/test/group-turns.test.mjs';
import '../dsh-director/test/catalog.test.mjs';
import '../dsh-director/test/confirm.test.mjs';
import '../dsh-director/test/connections.test.mjs';
import '../dsh-director/test/media.test.mjs';
import { walk } from '../dsh-director/test/boundary.test.mjs';
import { adapterVocabulary, adapterPattern } from './plugin-vocabulary.mjs';

const pluginRoot = fileURLToPath(new URL('../dsh-director/', import.meta.url));
// This file's own text, read for the imports that decide what runs here (test 3).
const ownSource = readFileSync(fileURLToPath(import.meta.url), 'utf8');

test('the adapter vocabulary is derived from its files, not empty', () => {
  const words = adapterVocabulary();
  for (const expected of ['state_read', 'send_user_message', 'project_work', 'record_evidence']) assert.ok(words.includes(expected), expected);
  assert.ok(words.length >= 15, words.join(', '));
});

test('no byte of any plugin file carries a name this adapter registers, adds or persists', () => {
  const found = [], pattern = adapterPattern();
  for (const file of walk()) readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
    const hit = pattern.exec(line);
    if (hit) found.push(`${relative(pluginRoot, file)}:${i + 1}: ${hit[0]}`);
  });
  assert.deepEqual(found, []);
});

// The plugin's declared runner. `npm test` in integrations/dsh-director is the list this
// repository trusts, and until this test nothing in the gate executed five of its files.
const declaredScript = () => {
  const pkg = JSON.parse(readFileSync(join(pluginRoot, 'package.json'), 'utf8'));
  return String(pkg.scripts?.test ?? '').trim();
};

/** The rule integrations/dsh-driver/suite-composition.test.mjs applies to this adapter's
 *  own script, applied to the plugin's with one difference the plugin's tree forces: the
 *  plugin's script must NAME its test files. A chained command and a glob are REFUSED
 *  because a chain can exit 0 while a test failed and a glob runs fewer files than it
 *  looks like. Bare discovery is REFUSED as well, and for the same kind of reason: node's
 *  discovery runs everything under a directory named test/, which here includes
 *  test/fixtures/memory-crash.mjs - a fixture that is SIGKILLed, not a test. Returns the
 *  named files. */
const checkScriptShape = script => {
  assert.ok(!/[;&|]/.test(script), `the plugin's test script chains commands or swallows failures (${script}). Write one bare \`node --test test/<file>.mjs ...\` invocation: a chain can exit 0 while a test failed.`);
  assert.ok(/^node\s+--test(\s|$)/.test(script), `the plugin's test script must be a \`node --test\` invocation (${script}). Write \`node --test test/<file>.mjs ...\`, naming every test file.`);
  const args = script.replace(/^node\s+--test\s*/, '').trim().split(/\s+/).filter(Boolean);
  assert.notDeepEqual(args, [], `the plugin's test script uses bare \`node --test\` discovery (${script}). Node's discovery runs every file under a directory named test/, which here includes test/fixtures/memory-crash.mjs - a fixture that is SIGKILLed, not a test: name every test file explicitly, \`node --test test/<file>.mjs ...\`.`);
  const globs = args.filter(arg => /[*?[\]]/.test(arg));
  assert.deepEqual(globs, [], `the plugin's test script passes a glob (${globs.join(', ')}). A partial glob runs fewer files than it looks like: name every test file explicitly, \`node --test test/<file>.mjs ...\`.`);
  return args;
};

/** The plugin files an import statement of THIS module executes, as plugin-relative
 *  paths. An ESM import runs the imported module's top level, and node:test
 *  registrations happen there, so a side-effect import and a named import of the same
 *  file both execute its tests. Derived from this file's own source on purpose: a
 *  hand-written copy of the plugin's file list would be the second list this whole
 *  arrangement exists to avoid. Stated limit: a specifier this file assembles at run
 *  time (a template literal, a loop over names) is invisible here - write the import. */
const executedByThisSuite = () => {
  const code = ownSource.split('\n').filter(line => !/^\s*(\/\/|\*|\/\*)/.test(line)).join('\n');
  const specifiers = [...code.matchAll(/^[ \t]*import\s+(?:[^'";]+?\s+from\s+)?['"]([^'"]+)['"];?[ \t]*$/gm)].map(match => match[1]);
  return new Set(specifiers.filter(specifier => specifier.startsWith('.'))
    .map(specifier => relative(pluginRoot, fileURLToPath(new URL(specifier, import.meta.url)))));
};

test('every test file the plugin\'s own script names is executed by this suite', () => {
  const declared = checkScriptShape(declaredScript());
  const executed = executedByThisSuite();
  const named = declared.map(arg => relative(pluginRoot, resolve(pluginRoot, arg)));
  const missing = named.filter(file => !executed.has(file)).sort();
  assert.deepEqual(missing, [], `integrations/dsh-director/package.json's "test" script declares files this suite does not execute: ${missing.join(', ')}. A file the plugin's own runner names and the repository gate never runs is a file whose failures nobody sees: add one side-effect import line at the top of integrations/dsh-driver/plugin-boundary.test.mjs for each file above.`);
});
