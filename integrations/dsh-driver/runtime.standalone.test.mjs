/** Plugin step 5 on the measured DSH pin: dsh-director with its built-in local host
 *  and no AItelier delivers two work items through two native workers (see
 *  ../dsh-director/standalone/scenario.mjs; run-in-container.sh runs the same
 *  scenario in a container holding nothing but DSH and the plugin). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { measuredEntryForTest } from './dsh-entry.mjs';
import { runStandalone } from '../dsh-director/standalone/scenario.mjs';
import { adapterPattern } from './plugin-vocabulary.mjs';

/** The todo CLI needs PyYAML: the first interpreter that has it, or a loud refusal. */
function pythonWithYaml() {
  for (const candidate of [process.env.STANDALONE_PYTHON, 'python3', '/usr/bin/python3'].filter(Boolean)) {
    try { execFileSync(candidate, ['-c', 'import yaml'], { stdio: 'ignore' }); return candidate; } catch { /* next */ }
  }
  throw new Error('no python3 with PyYAML found (set STANDALONE_PYTHON)');
}

test('real pinned DSH: dsh-director alone plans two work items, two native workers deliver them, the director finishes both', { timeout: 240000 }, async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const result = await runStandalone(entry, { python: pythonWithYaml() });
  assert.ok(result.ok, JSON.stringify(result, null, 2));
  assert.equal(result.workers.length, 2);
  // What the model received in this backend-free run carries none of THIS adapter's names
  // either (tool names it registers or adds, its MCP prefix, its source kinds).
  const pattern = adapterPattern();
  const carried = result.texts.map((text, i) => [i, pattern.exec(text)?.[0]]).filter(([, hit]) => hit);
  assert.deepEqual(carried, [], 'adapter names in model-facing text');
});
