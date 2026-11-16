/** The dsh-director plugin as seen from this adapter, inside the repository's gate.
 *
 *  1. Runs the plugin's own test files in this suite (they import nothing of the
 *     adapter), so `node --test` here - which tests/integration/
 *     test_driver_node_suite_runs.py runs - fails when the plugin's suite does.
 *  2. Checks the plugin against the vocabulary this adapter ACTUALLY has, derived
 *     rather than listed: every tool name runtime.mjs registers, every tool name
 *     aitelier-roles.mjs adds (with and without its MCP prefix), that MCP prefix,
 *     and the source kinds and section prefix of aitelier-texts.mjs. None of them
 *     may appear in any byte of any file the plugin's boundary walk visits. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import '../dsh-director/test/host.test.mjs';
import '../dsh-director/test/local-host.test.mjs';
import '../dsh-director/test/outbox.test.mjs';
import '../dsh-director/test/request-bodies.test.mjs';
import { walk } from '../dsh-director/test/boundary.test.mjs';
import { adapterVocabulary, adapterPattern } from './plugin-vocabulary.mjs';

const pluginRoot = fileURLToPath(new URL('../dsh-director/', import.meta.url));

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
