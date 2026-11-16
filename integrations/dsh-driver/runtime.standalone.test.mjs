/** Plugin step 5 on the measured DSH pin, inside the repository gate: dsh-director with
 *  its built-in local host and no backend delivers two work items through two workers
 *  (../dsh-director/standalone/scenario.mjs).
 *
 *  Isolation is part of the measurement: the plugin directory is COPIED to a fresh
 *  temporary directory outside this repository and the scenario runs from that copy,
 *  so a plugin file that reached for ../dsh-driver, ../../core or any other repository
 *  path would fail here instead of silently using the checkout. (run-in-container.sh
 *  additionally runs it where the image's application code is masked.) */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { cp, mkdtemp, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { measuredEntryForTest } from './dsh-entry.mjs';
import { adapterPattern } from './plugin-vocabulary.mjs';

const plugin = fileURLToPath(new URL('../dsh-director/', import.meta.url));

/** The todo CLI needs PyYAML: the first interpreter that has it, or a loud refusal. */
function pythonWithYaml() {
  for (const candidate of [process.env.STANDALONE_PYTHON, 'python3', '/usr/bin/python3'].filter(Boolean)) {
    try { execFileSync(candidate, ['-c', 'import yaml'], { stdio: 'ignore' }); return candidate; } catch { /* next */ }
  }
  throw new Error('no python3 with PyYAML found (set STANDALONE_PYTHON)');
}

test('real pinned DSH: dsh-director alone, copied out of the repository, plans two work items, two native workers deliver them, the director finishes both', { timeout: 240000 }, async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const isolated = await mkdtemp(join(tmpdir(), 'dsh-director-isolated-'));
  try {
    const copy = join(isolated, 'dsh-director');
    await cp(plugin, copy, { recursive: true, verbatimSymlinks: true, filter: source => !source.includes('node_modules') });
    assert.deepEqual((await readdir(isolated)).sort(), ['dsh-director'], 'the copy has no sibling to reach for');
    const { runStandalone } = await import(pathToFileURL(join(copy, 'standalone', 'scenario.mjs')).href);
    const result = await runStandalone(entry, { python: pythonWithYaml() });
    assert.ok(result.ok, JSON.stringify({ ...result, texts: undefined }, null, 2));
    assert.equal(result.workers.length, 2);
    // What the model received in this backend-free run carries none of THIS adapter's names
    // either (tool names it registers or adds, its MCP prefix, its source kinds).
    const pattern = adapterPattern();
    const carried = result.texts.map((text, i) => [i, pattern.exec(text)?.[0]]).filter(([, hit]) => hit);
    assert.deepEqual(carried, [], 'adapter names in model-facing text');
  } finally { await rm(isolated, { recursive: true, force: true }); }
});
